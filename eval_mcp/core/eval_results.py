"""Build and persist pre-computed eval result JSON for fast reads.

Reads Inspect AI .eval log files once and writes lightweight JSON that the
comparison API can serve directly without re-parsing.
"""

import json
import logging
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from inspect_ai._view.common import list_eval_logs_async
from inspect_ai.log import read_eval_log_async

from eval_mcp.core.pricing import calculate_cost
from eval_mcp.core.user_storage import (
    get_user_base_dir,
    get_user_dir,
    get_user_log_dir,
    load_eval_detail,
    load_eval_groups,
    save_eval_detail,
    save_eval_groups,
)

logger = logging.getLogger(__name__)


# Provider systems whose spans represent on-the-wire LLM calls. When a span
# tagged with one of these is present for a given model, treat its tokens as
# canonical and discard any framework-layer tokens for the same model.
#
# OTel GenAI semconv values that botocore's Bedrock instrumentation uses; if
# you add another provider (anthropic SDK, openai SDK, etc.), append its
# `gen_ai.system` value here.
_PROVIDER_SYSTEMS = ("aws.bedrock",)


def _split_model_key(model_key: str) -> tuple[str, str]:
    """Split an Inspect ModelEvent.model string into (system, model_id).

    bedrock_capture.py builds the model string as f"{provider}/{model}" where
    provider is the OTel `gen_ai.system` attr and model is `gen_ai.request.model`.
    Example: "aws.bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0".

    Returns ("", model_key) if there's no slash separator (defensive fallback).
    """
    if "/" not in model_key:
        return "", model_key
    system, _, model_id = model_key.partition("/")
    return system, model_id


def _dedupe_layered_model_usage(usage_by_key: dict[str, dict]) -> dict[str, dict]:
    """Collapse provider+framework duplicates that refer to the same model.

    Strands, LangChain, and other agent frameworks that self-instrument emit
    their own GenAI spans alongside botocore's Bedrock spans. Both spans cover
    the same converse() call but the framework span typically reports only the
    first turn's tokens (it doesn't sum across the tool-use loop), while the
    provider span reports per-HTTP-call tokens — the actual ground truth.

    Rule: when the same model_id appears under both a framework system and a
    provider system, keep the provider's tokens and drop the framework entry.
    The framework's role is preserved at the trace level (you can still see
    "this call was inside a strands.Agent.invoke") but it stops double-counting
    in the "Models used" aggregation.

    If a model only has framework-layer spans (no provider span — e.g.
    framework hit a non-Bedrock backend), keep the framework entry as-is so
    we don't silently lose data.
    """
    if not usage_by_key:
        return {}

    # Index by model_id so we can see what systems exist per model.
    by_model_id: dict[str, list[tuple[str, str, dict]]] = defaultdict(list)
    for key, usage in usage_by_key.items():
        system, model_id = _split_model_key(key)
        by_model_id[model_id].append((system, key, usage))

    deduped: dict[str, dict] = {}
    for model_id, entries in by_model_id.items():
        provider_entries = [e for e in entries if e[0] in _PROVIDER_SYSTEMS]
        if provider_entries:
            # Provider tokens are ground truth — keep all provider-systems' rows
            # (one per provider, normally just aws.bedrock) and drop framework
            # rows for this model_id.
            for system, key, usage in provider_entries:
                deduped[key] = usage
        else:
            # No provider span — preserve whatever we have. This is the
            # "framework hit a backend we don't instrument" case.
            for system, key, usage in entries:
                deduped[key] = usage
    return deduped


async def _read_log_headers(log_dir: str) -> list[dict]:
    eval_log_infos = await list_eval_logs_async(log_dir)
    results = []
    for info in eval_log_infos:
        try:
            log = await read_eval_log_async(info.name, header_only=True)
            entry = {
                "file": info.name,
                "run_id": log.eval.run_id if log.eval.run_id else None,
                "task": log.eval.task,
                "task_file": log.eval.task_file if hasattr(log.eval, "task_file") else None,
                "model": log.eval.model,
                "status": log.status,
                "created": log.eval.created,
                "dataset_samples": log.eval.dataset.samples if log.eval.dataset else 0,
            }
            if log.results and log.results.scores:
                entry["scores"] = [
                    {"name": s.name, "metrics": {n: m.value for n, m in s.metrics.items()}}
                    for s in log.results.scores
                ]
            if log.stats:
                usage = {}
                if log.stats.model_usage:
                    for model_name, mu in log.stats.model_usage.items():
                        usage[model_name] = {
                            "input_tokens": mu.input_tokens,
                            "output_tokens": mu.output_tokens,
                            "total_tokens": mu.total_tokens,
                        }
                entry["model_usage"] = usage
                if log.stats.started_at:
                    entry["started_at"] = str(log.stats.started_at)
                if log.stats.completed_at:
                    entry["completed_at"] = str(log.stats.completed_at)
            results.append(entry)
        except Exception as e:
            logger.warning(f"Failed to read log {info.name}: {e}")
    return results


async def _read_full_logs(log_files: list[str]) -> list[dict]:
    results = []
    for f in log_files:
        try:
            log = await read_eval_log_async(f, header_only=False)
            entry: dict = {
                "file": f,
                "task": log.eval.task,
                "model": log.eval.model,
                "status": log.status,
                "samples": [],
            }
            agent_usage: dict[str, dict] = {}
            if log.samples:
                for s in log.samples:
                    sample: dict = {
                        "id": str(s.id),
                        "input": str(s.input) if isinstance(s.input, str) else str(s.input[0].content if s.input else ""),
                        "target": s.target[0] if isinstance(s.target, list) else str(s.target) if s.target else "",
                        "output": s.output.completion if s.output else "",
                    }
                    if s.scores:
                        sample["scores"] = {}
                        for scorer_name, score in s.scores.items():
                            score_data: dict = {
                                "value": str(score.value),
                                "explanation": score.explanation or "",
                            }
                            if score.metadata:
                                score_data["metadata"] = score.metadata
                            sample["scores"][scorer_name] = score_data
                    if s.model_usage:
                        sample["model_usage"] = {
                            k: {"input_tokens": v.input_tokens, "output_tokens": v.output_tokens, "total_tokens": v.total_tokens}
                            for k, v in s.model_usage.items()
                        }
                    if hasattr(s, 'events') and s.events:
                        solver_spans = set()
                        for ev in s.events:
                            etype = type(ev).__name__
                            if etype == "SpanBeginEvent":
                                if getattr(ev, "type", "") in ("solver", "agent"):
                                    solver_spans.add(ev.id)
                                elif hasattr(ev, "parent_id") and ev.parent_id in solver_spans:
                                    solver_spans.add(ev.id)
                        for ev in s.events:
                            if type(ev).__name__ == "ModelEvent" and ev.span_id in solver_spans:
                                if hasattr(ev, "output") and ev.output and ev.output.usage:
                                    m = ev.model
                                    if m not in agent_usage:
                                        agent_usage[m] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
                                    agent_usage[m]["input_tokens"] += ev.output.usage.input_tokens
                                    agent_usage[m]["output_tokens"] += ev.output.usage.output_tokens
                                    agent_usage[m]["total_tokens"] += ev.output.usage.input_tokens + ev.output.usage.output_tokens
                    entry["samples"].append(sample)
            entry["agent_model_usage"] = _dedupe_layered_model_usage(agent_usage)
            results.append(entry)
        except Exception as e:
            logger.warning(f"Failed to read full log {f}: {e}")
    return results


def _load_criteria_descriptions(user_dir: Path, task_name: str, criteria_names: set[str]) -> dict[str, str]:
    """Find a config whose criteria cover all names we care about; return {name: description}.

    Supports both standard configs (top-level `criteria`) and pipeline agent
    configs (nested under `pipeline_stages.stages[].criteria`).
    """
    base_real = os.path.realpath(str(get_user_base_dir()))
    configs_real = os.path.realpath(str(user_dir / "configs"))
    if not configs_real.startswith(base_real + os.sep):
        raise ValueError(f"path escape attempt: {configs_real}")
    if not os.path.isdir(configs_real):
        return {}
    for json_file in Path(configs_real).glob("*.json"):
        json_real = os.path.realpath(str(json_file))
        if not json_real.startswith(base_real + os.sep):
            raise ValueError(f"path escape attempt: {json_real}")
        try:
            with open(json_real, "r") as f:
                data = json.loads(f.read())
            # Collect all {name: description} from either layout
            merged: dict[str, str] = {}
            for c in data.get("criteria", []) or []:
                if c.get("name") and c.get("description"):
                    merged[c["name"]] = c["description"]
            pipeline = (data.get("pipeline_stages") or {}).get("stages") or []
            for stage in pipeline:
                for c in (stage.get("criteria") or []):
                    if c.get("name") and c.get("description"):
                        merged[c["name"]] = c["description"]
            if criteria_names and criteria_names.issubset(merged.keys()):
                return {name: merged[name] for name in criteria_names}
        except Exception:
            continue
    return {}


def _build_groups_from_headers(headers: list[dict]) -> dict:
    groups_map: dict[str, list[dict]] = defaultdict(list)
    for log in headers:
        key = log.get("run_id") or log["file"]
        groups_map[key].append(log)

    groups = []
    for run_id, run_logs in groups_map.items():
        distinct_tasks = list(dict.fromkeys(l.get("task", "") for l in run_logs))
        is_prompt_comparison = len(distinct_tasks) > 1
        models = list(dict.fromkeys(l["model"] for l in run_logs))

        scores_by_key = {}
        for l in run_logs:
            if l.get("scores"):
                metrics = {}
                for s in l["scores"]:
                    metrics.update(s["metrics"])
                if is_prompt_comparison:
                    scores_by_key[f"{l.get('task', '')}/{l['model']}"] = metrics
                else:
                    scores_by_key[l["model"]] = metrics

        task_name = run_logs[0].get("task", "unknown")
        config_name = task_name.replace("eval_task", "").strip("_") or task_name

        status = run_logs[0].get("status", "unknown")
        if status == "error" and run_logs[0].get("dataset_samples", 0) > 0:
            status = "completed"

        group = {
            "id": run_id,
            "task": task_name,
            "configName": config_name,
            "created": run_logs[0].get("created", ""),
            "models": models,
            "sampleCount": run_logs[0].get("dataset_samples", 0),
            "status": status,
            "scores": scores_by_key,
        }
        if is_prompt_comparison:
            group["promptComparison"] = True
            group["promptCount"] = len(distinct_tasks)

        groups.append(group)

    groups.sort(key=lambda g: g["created"], reverse=True)
    return {"groups": groups}


def _build_detail_from_logs(
    group_id: str,
    group_logs: list[dict],
    full_logs: list[dict],
    user_dir: Path,
) -> dict:
    # Detect prompt comparison: multiple distinct task names in one group
    distinct_tasks = list(dict.fromkeys(l.get("task", "") for l in full_logs))
    is_prompt_comparison = len(distinct_tasks) > 1

    if is_prompt_comparison:
        models = list(dict.fromkeys(f"{l.get('task', '')}/{l['model']}" for l in full_logs))
        # Sort by model first, then prompt number — same model side by side: P1 P2 P1 P2
        def _col_sort_key(k: str) -> tuple:
            task_part, model_part = k.split("/", 1)
            # Extract number from eval_N
            num = int(task_part.replace("eval_", "")) if task_part.startswith("eval_") else 0
            return (model_part, num)
        models.sort(key=_col_sort_key)
    else:
        models = [l["model"] for l in full_logs]

    samples_by_id: dict[str, dict] = {}
    criteria_set: set[str] = set()
    pipeline_stages: list[dict] = []
    is_pipeline = False

    for log in full_logs:
        if is_prompt_comparison:
            column_key = f"{log.get('task', '')}/{log['model']}"
        else:
            column_key = log["model"]
        for sample in log.get("samples", []):
            sid = sample["id"]
            if sid not in samples_by_id:
                samples_by_id[sid] = {
                    "id": sid,
                    "input": sample["input"],
                    "target": sample["target"],
                    "results": {},
                }

            score_data: dict = {"passed": False, "score": 0.0, "output": sample.get("output", "")}
            if sample.get("scores"):
                scorers = sample["scores"]
                stage_scorers = {k: v for k, v in scorers.items() if k.startswith("stage_")}
                if stage_scorers:
                    is_pipeline = True
                    all_passed = all(s["value"] == "C" for s in stage_scorers.values())
                    score_data["passed"] = all_passed
                    score_data["score"] = sum(1 for s in stage_scorers.values() if s["value"] == "C") / len(stage_scorers)

                    stages_data = {}
                    for scorer_name, score in stage_scorers.items():
                        stage_name = scorer_name.replace("stage_", "")
                        metadata = score.get("metadata", {})
                        stage_result = {
                            "passed": score["value"] == "C",
                            "explanation": score.get("explanation", ""),
                            "stage_order": metadata.get("stage_order", 0),
                        }
                        if "tools_called" in metadata:
                            stage_result["tools_called"] = metadata["tools_called"]
                            stage_result["tools_expected"] = metadata.get("tools_expected", [])
                        criteria_results = metadata.get("criteria_results", [])
                        if criteria_results:
                            stage_result["criteriaResults"] = criteria_results
                            for cr in criteria_results:
                                criteria_set.add(cr["name"])
                        stages_data[stage_name] = stage_result

                    score_data["stages"] = stages_data
                    all_criteria = []
                    for stage in stages_data.values():
                        all_criteria.extend(stage.get("criteriaResults", []))
                    score_data["criteriaResults"] = all_criteria
                else:
                    # Multi-scorer composition (e.g. jury + f1): prefer
                    # jury_scorer for the primary score + criteria breakdown
                    # the UI is designed around. Other scorers are captured
                    # in scoresByScorer so the frontend can surface them
                    # alongside without losing data.
                    primary_name = "jury_scorer" if "jury_scorer" in scorers else next(iter(scorers))
                    scores_by_scorer: dict[str, float] = {}
                    for scorer_name, score in scorers.items():
                        metadata = score.get("metadata", {})
                        raw_value = score.get("value")
                        # _read_full_logs stringifies every score via
                        # ``str(score.value)`` so we get "0.473" instead
                        # of 0.473 for built-in scorers like f1. Coerce
                        # back to float when possible; only fall back to
                        # the jury_score metadata / "C" sentinel for the
                        # categorical-value path (which is what the LLM
                        # judges return).
                        sample_score: float
                        if isinstance(raw_value, (int, float)):
                            sample_score = float(raw_value)
                        else:
                            try:
                                sample_score = float(raw_value)
                            except (TypeError, ValueError):
                                sample_score = metadata.get(
                                    "jury_score",
                                    1.0 if raw_value == "C" else 0.0,
                                )
                        scores_by_scorer[scorer_name] = sample_score
                        if scorer_name == primary_name:
                            score_data["score"] = sample_score
                            score_data["passed"] = sample_score > 0.5
                            score_data["explanation"] = score.get("explanation", "")
                            criteria_results = metadata.get("criteria_results", [])
                            score_data["criteriaResults"] = criteria_results
                            for cr in criteria_results:
                                criteria_set.add(cr["name"])
                    # Always include scoresByScorer when we have any
                    # entry — the frontend uses it not just for the
                    # multi-scorer chip row but also to label
                    # single non-jury scorer runs ("this 47/100 is an
                    # F1 score"). For jury-only runs the frontend
                    # ignores it.
                    if scores_by_scorer:
                        score_data["scoresByScorer"] = scores_by_scorer

            samples_by_id[sid]["results"][column_key] = score_data

    if is_pipeline:
        first_sample = next(iter(samples_by_id.values()), None)
        if first_sample:
            first_result = next(iter(first_sample["results"].values()), None)
            if first_result and "stages" in first_result:
                for stage_name, stage_data in sorted(
                    first_result["stages"].items(),
                    key=lambda x: x[1].get("stage_order", 0)
                ):
                    stage_info = {
                        "name": stage_name,
                        "displayName": stage_name.replace("_", " ").title(),
                        "order": stage_data.get("stage_order", 0),
                        "scorerType": "deterministic" if "tools_called" in stage_data else "llm_judge",
                        "criteria": [cr["name"] for cr in stage_data.get("criteriaResults", [])],
                    }
                    pipeline_stages.append(stage_info)

    aggregate: dict[str, dict] = {}
    for model in models:
        model_samples = [
            s["results"][model]
            for s in samples_by_id.values()
            if model in s["results"]
        ]
        total = len(model_samples)

        by_criterion: dict[str, float] = {}
        for criterion in criteria_set:
            crit_values: list[float] = []
            for s in model_samples:
                for cr in s.get("criteriaResults", []):
                    if cr["name"] != criterion:
                        continue
                    if "score" in cr:
                        crit_values.append(float(cr["score"]))
                    elif cr.get("total", 0) > 0:
                        crit_values.append(cr["votes_for"] / cr["total"])
                    else:
                        crit_values.append(1.0 if cr.get("passed") else 0.0)
            by_criterion[criterion] = sum(crit_values) / len(crit_values) if crit_values else 0.0

        by_stage: dict[str, float] = {}
        if is_pipeline and pipeline_stages:
            for stage_info in pipeline_stages:
                stage_name = stage_info["name"]
                stage_criteria = stage_info.get("criteria", [])
                if stage_criteria:
                    stage_criteria_scores = [by_criterion.get(c, 0) for c in stage_criteria]
                    by_stage[stage_name] = sum(stage_criteria_scores) / len(stage_criteria_scores)
                else:
                    stage_passed = 0
                    for s in model_samples:
                        stage_data = s.get("stages", {}).get(stage_name)
                        if stage_data and stage_data.get("passed"):
                            stage_passed += 1
                    by_stage[stage_name] = stage_passed / max(total, 1)

        # Per-scorer aggregate from each sample's scoresByScorer (only set
        # when the eval ran more than one scorer, e.g. ["jury", "f1"]).
        # Lets the UI surface "f1=0.51, exact=0.0" alongside the jury
        # headline so customers can see every signal the run produced.
        by_scorer: dict[str, float] = {}
        scorer_buckets: dict[str, list[float]] = {}
        for s in model_samples:
            sbs = s.get("scoresByScorer") or {}
            for name, v in sbs.items():
                try:
                    scorer_buckets.setdefault(name, []).append(float(v))
                except (TypeError, ValueError):
                    continue
        for name, vals in scorer_buckets.items():
            if vals:
                by_scorer[name] = sum(vals) / len(vals)

        if by_stage:
            overall = sum(by_stage.values()) / len(by_stage) if by_stage else 0
        elif by_criterion:
            overall = sum(by_criterion.values()) / len(by_criterion)
        elif total > 0:
            # No jury (no criteria) — headline is the mean of per-sample
            # scores. Previously this fell through to 0 when only
            # deterministic scorers were used (f1/exact/...), which
            # produced a misleading "0%" headline regardless of the real
            # scorer output. Now it tells the truth.
            overall = sum(float(s.get("score") or 0) for s in model_samples) / total
        else:
            overall = 0

        agg = {"overall": overall, "byCriterion": by_criterion}
        if by_stage:
            agg["byStage"] = by_stage
        if by_scorer:
            agg["byScorer"] = by_scorer
        aggregate[model] = agg

    task_name = group_logs[0].get("task", "")

    stats: dict[str, dict] = {}
    for log_header in group_logs:
        raw_model = log_header["model"]
        if is_prompt_comparison:
            stat_key = f"{log_header.get('task', '')}/{raw_model}"
        else:
            stat_key = raw_model
        started = log_header.get("started_at")
        completed = log_header.get("completed_at")

        latency_seconds = None
        if started and completed:
            try:
                t0 = datetime.fromisoformat(started)
                t1 = datetime.fromisoformat(completed)
                latency_seconds = (t1 - t0).total_seconds()
            except (ValueError, TypeError):
                pass

        sample_count = log_header.get("dataset_samples", 1) or 1
        avg_latency = latency_seconds / sample_count if latency_seconds else None

        stats[stat_key] = {
            "startedAt": started,
            "completedAt": completed,
            "totalSeconds": latency_seconds,
            "latencySeconds": round(avg_latency, 2) if avg_latency else None,
        }

        full_log = next((fl for fl in full_logs if fl.get("model") == raw_model and fl.get("task") == log_header.get("task")), None)
        agent_usage = full_log.get("agent_model_usage", {}) if full_log else {}

        if agent_usage:
            agent_input = 0
            agent_output = 0
            agent_tokens = 0
            agent_cost = 0.0
            per_model = {}
            for model_name, usage in agent_usage.items():
                inp = usage["input_tokens"]
                out = usage["output_tokens"]
                tot = usage["total_tokens"]
                agent_input += inp
                agent_output += out
                agent_tokens += tot
                model_cost = calculate_cost(model_name, inp, out)
                if model_cost is not None:
                    agent_cost += model_cost
                per_model[model_name] = {
                    "input_tokens": inp,
                    "output_tokens": out,
                    "total_tokens": tot,
                    "cost": model_cost,
                }
            stats[stat_key]["input_tokens"] = agent_input
            stats[stat_key]["output_tokens"] = agent_output
            stats[stat_key]["total_tokens"] = agent_tokens
            stats[stat_key]["cost"] = agent_cost
            stats[stat_key]["modelUsage"] = per_model
        elif log_header.get("model_usage"):
            total_input = 0
            total_output = 0
            total_tokens = 0
            for usage in log_header["model_usage"].values():
                total_input += usage.get("input_tokens", 0)
                total_output += usage.get("output_tokens", 0)
                total_tokens += usage.get("total_tokens", 0)
            stats[stat_key]["input_tokens"] = total_input
            stats[stat_key]["output_tokens"] = total_output
            stats[stat_key]["total_tokens"] = total_tokens
            stats[stat_key]["cost"] = calculate_cost(raw_model, total_input, total_output)

            if latency_seconds and latency_seconds > 0:
                stats[stat_key]["tokensPerSecond"] = round(total_output / latency_seconds, 1)

    criteria_descriptions = _load_criteria_descriptions(user_dir, task_name, criteria_set)

    # For agent evals, replace model name with agent image name
    agent_image = None
    config_data = None
    base_real = os.path.realpath(str(get_user_base_dir()))
    configs_real = os.path.realpath(str(user_dir / "configs"))
    if not configs_real.startswith(base_real + os.sep):
        raise ValueError(f"path escape attempt: {configs_real}")
    if os.path.isdir(configs_real):
        task_file = group_logs[0].get("task_file") if group_logs else None
        if task_file:
            config_filename = Path(task_file).with_suffix(".json").name
        else:
            config_filename = f"{config_name}.json"
        # Filename is user-derived (from log metadata / config name); strip any
        # path components via basename before joining so the result stays under
        # configs_real.
        config_basename = os.path.basename(config_filename)
        config_json_real = os.path.realpath(os.path.join(configs_real, config_basename))
        if not config_json_real.startswith(configs_real + os.sep):
            raise ValueError(f"path escape attempt: {config_json_real}")
        if os.path.isfile(config_json_real):
            try:
                with open(config_json_real, "r") as f:
                    config_data = json.loads(f.read())
                if config_data.get("agent_image"):
                    agent_image = config_data["agent_image"]
            except Exception:
                pass

    display_models = models
    score_only = bool(config_data and config_data.get("score_only"))
    if agent_image and len(models) == 1:
        display_models = [f"agent/{agent_image}"]
        old_model = models[0]
        if old_model in aggregate:
            aggregate[f"agent/{agent_image}"] = aggregate.pop(old_model)
        if old_model in stats:
            stats[f"agent/{agent_image}"] = stats.pop(old_model)
        for sample in samples_by_id.values():
            if old_model in sample.get("results", {}):
                sample["results"][f"agent/{agent_image}"] = sample["results"].pop(old_model)
    elif score_only and len(models) == 1:
        # Inspect AI logs the model as "none/none" when no --model is
        # passed. That literal isn't user-facing — relabel for the
        # viewer so the column header reads "pre-generated".
        score_only_label = "pre-generated"
        display_models = [score_only_label]
        old_model = models[0]
        if old_model in aggregate:
            aggregate[score_only_label] = aggregate.pop(old_model)
        if old_model in stats:
            stats[score_only_label] = stats.pop(old_model)
        for sample in samples_by_id.values():
            if old_model in sample.get("results", {}):
                sample["results"][score_only_label] = sample["results"].pop(old_model)

    result = {
        "groupId": group_id,
        "task": task_name,
        "models": display_models,
        "criteria": sorted(criteria_set),
        "criteriaDescriptions": criteria_descriptions,
        "aggregate": aggregate,
        "samples": list(samples_by_id.values()),
        "stats": stats,
    }
    if pipeline_stages:
        result["pipeline"] = pipeline_stages
    if agent_image:
        result["agentImage"] = agent_image
    if score_only:
        result["scoreOnly"] = True
    if config_data and config_data.get("prompts"):
        result["prompts"] = config_data["prompts"]
    return result


async def precompute_eval_results(user_id: str, force: bool = False) -> None:
    """Parse all .eval files for a user and save pre-computed JSON to S3/disk.

    Called after an eval completes and by the migration script.
    """
    log_dir = get_user_log_dir(user_id)
    user_dir = get_user_dir(user_id)

    headers = await _read_log_headers(log_dir)
    if not headers:
        return

    groups_response = _build_groups_from_headers(headers)
    save_eval_groups(user_id, groups_response)
    logger.info(f"Saved pre-computed groups JSON for user {user_id} ({len(groups_response['groups'])} groups)")

    for group in groups_response["groups"]:
        group_id = group["id"]

        if not force:
            existing = load_eval_detail(user_id, group_id)
            if existing and group.get("status") == "success":
                continue

        group_logs = [h for h in headers if (h.get("run_id") or h["file"]) == group_id]
        if not group_logs:
            continue

        try:
            log_files = [l["file"] for l in group_logs]
            full_logs = await _read_full_logs(log_files)
            if not full_logs:
                continue
            detail_response = _build_detail_from_logs(group_id, group_logs, full_logs, user_dir)
            save_eval_detail(user_id, group_id, detail_response)
            logger.info(f"Saved pre-computed detail JSON for group {group_id}")
        except Exception as e:
            logger.warning(f"Failed to pre-compute detail for group {group_id}: {e}")
