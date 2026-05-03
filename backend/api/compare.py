"""Comparison API for viewing evaluation results across multiple models.

Uses read_eval_log_async() directly since FastAPI endpoints are already
in an async context — no subprocess or nest_asyncio needed.

Caching: groups list and last N eval details are cached in memory per user.
Cache is invalidated when a new eval completes.
"""

import asyncio
import json
import logging
import time
from collections import OrderedDict, defaultdict
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from inspect_ai._view.common import list_eval_logs_async
from inspect_ai.log import read_eval_log_async

from backend.core.pricing import calculate_cost
from backend.core.user_storage import get_user_dir, get_user_log_dir

logger = logging.getLogger(__name__)

router = APIRouter()

# Per-user cache: groups response and detail responses (LRU, max 10 details per user)
_DETAIL_CACHE_SIZE = 10

_groups_cache: dict[str, dict] = {}  # user_id -> {"data": response, "time": timestamp}
_detail_cache: dict[str, OrderedDict] = {}  # user_id -> OrderedDict{group_id -> response}


def invalidate_user_cache(user_id: str) -> None:
    """Invalidate all cached data for a user. Called after eval completion."""
    _groups_cache.pop(user_id, None)
    _detail_cache.pop(user_id, None)


async def _get_user_id(request: Request) -> str:
    user_id = request.headers.get("X-Forwarded-User")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


async def _read_log_headers(log_dir: str) -> list[dict]:
    """Read .eval log headers (no samples) from a directory (local or S3)."""
    eval_log_infos = await list_eval_logs_async(log_dir)
    results = []
    for info in eval_log_infos:
        try:
            log = await read_eval_log_async(info.name, header_only=True)
            entry = {
                "file": info.name,
                "run_id": log.eval.run_id if log.eval.run_id else None,
                "task": log.eval.task,
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
    """Read full .eval logs with samples."""
    results = []
    for f in log_files:
        try:
            log = await read_eval_log_async(f, header_only=False)
            entry: dict = {
                "file": f,
                "model": log.eval.model,
                "status": log.status,
                "samples": [],
            }
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
                    entry["samples"].append(sample)
            results.append(entry)
        except Exception as e:
            logger.warning(f"Failed to read full log {f}: {e}")
    return results


async def _build_groups_response(user_id: str) -> dict:
    """Build the groups response (fetches from S3/disk)."""
    log_dir = get_user_log_dir(user_id)
    logs = await _read_log_headers(log_dir)

    groups_map: dict[str, list[dict]] = defaultdict(list)
    for log in logs:
        key = log.get("run_id") or log["file"]
        groups_map[key].append(log)

    groups = []
    for run_id, run_logs in groups_map.items():
        models = [l["model"] for l in run_logs]
        scores_by_model = {}
        for l in run_logs:
            if l.get("scores"):
                metrics = {}
                for s in l["scores"]:
                    metrics.update(s["metrics"])
                scores_by_model[l["model"]] = metrics

        task_name = run_logs[0].get("task", "unknown")
        config_name = task_name.replace("eval_task", "").strip("_") or task_name

        # If Inspect marks as "error" but samples ran, it means some samples
        # errored (normal for agent evals) — show as "completed" not "error"
        status = run_logs[0].get("status", "unknown")
        if status == "error" and run_logs[0].get("dataset_samples", 0) > 0:
            status = "completed"

        groups.append({
            "id": run_id,
            "task": task_name,
            "configName": config_name,
            "created": run_logs[0].get("created", ""),
            "models": models,
            "sampleCount": run_logs[0].get("dataset_samples", 0),
            "status": status,
            "scores": scores_by_model,
        })

    groups.sort(key=lambda g: g["created"], reverse=True)
    return {"groups": groups}


async def _build_detail_response(user_id: str, group_id: str) -> dict:
    """Build the detail response for a group (fetches from S3/disk)."""
    user_dir = get_user_dir(user_id)
    log_dir = get_user_log_dir(user_id)

    headers = await _read_log_headers(log_dir)

    group_logs = [h for h in headers if h.get("run_id") == group_id]
    if not group_logs:
        group_logs = [h for h in headers if h["file"] == group_id]
    if not group_logs:
        raise HTTPException(status_code=404, detail="Group not found")

    log_files = [l["file"] for l in group_logs]
    full_logs = await _read_full_logs(log_files)

    if not full_logs:
        raise HTTPException(status_code=500, detail="Failed to read evaluation logs")

    models = [l["model"] for l in full_logs]

    samples_by_id: dict[str, dict] = {}
    criteria_set: set[str] = set()
    pipeline_stages: list[dict] = []
    is_pipeline = False

    for log in full_logs:
        model = log["model"]
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
                # Detect pipeline eval (multiple stage_* scorers)
                stage_scorers = {k: v for k, v in scorers.items() if k.startswith("stage_")}
                if stage_scorers:
                    is_pipeline = True
                    # All stages must pass for overall pass
                    all_passed = all(s["value"] == "C" for s in stage_scorers.values())
                    score_data["passed"] = all_passed
                    score_data["score"] = sum(1 for s in stage_scorers.values() if s["value"] == "C") / len(stage_scorers)

                    # Build per-stage results
                    stages_data = {}
                    for scorer_name, score in stage_scorers.items():
                        stage_name = scorer_name.replace("stage_", "")
                        metadata = score.get("metadata", {})
                        stage_result = {
                            "passed": score["value"] == "C",
                            "explanation": score.get("explanation", ""),
                            "stage_order": metadata.get("stage_order", 0),
                        }
                        # Deterministic stage data
                        if "tools_called" in metadata:
                            stage_result["tools_called"] = metadata["tools_called"]
                            stage_result["tools_expected"] = metadata.get("tools_expected", [])
                        # LLM judge stage data
                        criteria_results = metadata.get("criteria_results", [])
                        if criteria_results:
                            stage_result["criteriaResults"] = criteria_results
                            for cr in criteria_results:
                                criteria_set.add(cr["name"])
                        stages_data[stage_name] = stage_result

                    score_data["stages"] = stages_data
                    # Also keep flat criteriaResults for backward compat
                    all_criteria = []
                    for stage in stages_data.values():
                        all_criteria.extend(stage.get("criteriaResults", []))
                    score_data["criteriaResults"] = all_criteria
                else:
                    # Legacy single-scorer eval
                    for scorer_name, score in scorers.items():
                        score_data["passed"] = score["value"] == "C"
                        metadata = score.get("metadata", {})
                        score_data["score"] = metadata.get("jury_score", 1.0 if score["value"] == "C" else 0.0)
                        score_data["explanation"] = score.get("explanation", "")
                        criteria_results = metadata.get("criteria_results", [])
                        score_data["criteriaResults"] = criteria_results
                        for cr in criteria_results:
                            criteria_set.add(cr["name"])

            samples_by_id[sid]["results"][model] = score_data

    # Build pipeline stage metadata if detected
    if is_pipeline:
        # Extract stage info from first sample
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
            criterion_passed = 0
            for s in model_samples:
                for cr in s.get("criteriaResults", []):
                    if cr["name"] == criterion and cr["passed"]:
                        criterion_passed += 1
            by_criterion[criterion] = criterion_passed / max(total, 1)

        # Per-stage scores = average of their criteria scores
        by_stage: dict[str, float] = {}
        if is_pipeline and pipeline_stages:
            for stage_info in pipeline_stages:
                stage_name = stage_info["name"]
                stage_criteria = stage_info.get("criteria", [])
                if stage_criteria:
                    # Average of this stage's criteria pass rates
                    stage_criteria_scores = [by_criterion.get(c, 0) for c in stage_criteria]
                    by_stage[stage_name] = sum(stage_criteria_scores) / len(stage_criteria_scores)
                else:
                    # Deterministic stage — use pass rate from scorer directly
                    stage_passed = 0
                    for s in model_samples:
                        stage_data = s.get("stages", {}).get(stage_name)
                        if stage_data and stage_data.get("passed"):
                            stage_passed += 1
                    by_stage[stage_name] = stage_passed / max(total, 1)

        # Overall = average of all stage scores (pipeline) or all criteria (non-pipeline)
        if by_stage:
            overall = sum(by_stage.values()) / len(by_stage) if by_stage else 0
        elif by_criterion:
            overall = sum(by_criterion.values()) / len(by_criterion)
        else:
            overall = 0

        agg = {"overall": overall, "byCriterion": by_criterion}
        if by_stage:
            agg["byStage"] = by_stage
        aggregate[model] = agg

    stats: dict[str, dict] = {}
    for log in group_logs:
        model = log["model"]
        started = log.get("started_at")
        completed = log.get("completed_at")

        latency_seconds = None
        if started and completed:
            try:
                t0 = datetime.fromisoformat(started)
                t1 = datetime.fromisoformat(completed)
                latency_seconds = (t1 - t0).total_seconds()
            except (ValueError, TypeError):
                pass

        sample_count = log.get("dataset_samples", 1) or 1
        avg_latency = latency_seconds / sample_count if latency_seconds else None

        stats[model] = {
            "startedAt": started,
            "completedAt": completed,
            "totalSeconds": latency_seconds,
            "latencySeconds": round(avg_latency, 2) if avg_latency else None,
        }

        if log.get("model_usage"):
            total_input = 0
            total_output = 0
            total_tokens = 0
            for usage in log["model_usage"].values():
                total_input += usage.get("input_tokens", 0)
                total_output += usage.get("output_tokens", 0)
                total_tokens += usage.get("total_tokens", 0)
            stats[model]["input_tokens"] = total_input
            stats[model]["output_tokens"] = total_output
            stats[model]["total_tokens"] = total_tokens

            cost = calculate_cost(model, total_input, total_output)
            stats[model]["cost"] = cost

            if latency_seconds and latency_seconds > 0:
                stats[model]["tokensPerSecond"] = round(total_output / latency_seconds, 1)

    task_name = group_logs[0].get("task", "")
    criteria_descriptions = _load_criteria_descriptions(user_dir, task_name, criteria_set)

    result = {
        "groupId": group_id,
        "task": task_name,
        "models": models,
        "criteria": sorted(criteria_set),
        "criteriaDescriptions": criteria_descriptions,
        "aggregate": aggregate,
        "samples": list(samples_by_id.values()),
        "stats": stats,
    }
    if pipeline_stages:
        result["pipeline"] = pipeline_stages
    return result


@router.get("/groups")
async def get_comparison_groups(user_id: str = Depends(_get_user_id)):
    """List evaluation comparison groups for the user."""
    cached = _groups_cache.get(user_id)
    if cached:
        return cached["data"]

    response = await _build_groups_response(user_id)
    _groups_cache[user_id] = {"data": response, "time": time.time()}
    return response


@router.get("/detail")
async def get_comparison_detail(group_id: str, user_id: str = Depends(_get_user_id)):
    """Get full comparison data for a specific evaluation group."""
    user_details = _detail_cache.get(user_id)
    if user_details and group_id in user_details:
        user_details.move_to_end(group_id)
        return user_details[group_id]

    response = await _build_detail_response(user_id, group_id)

    if user_id not in _detail_cache:
        _detail_cache[user_id] = OrderedDict()
    _detail_cache[user_id][group_id] = response
    _detail_cache[user_id].move_to_end(group_id)

    while len(_detail_cache[user_id]) > _DETAIL_CACHE_SIZE:
        _detail_cache[user_id].popitem(last=False)

    return response


@router.post("/invalidate-cache/{user_id}")
async def invalidate_cache(user_id: str):
    """Invalidate cache for a user. Called internally after eval completion."""
    invalidate_user_cache(user_id)
    return {"ok": True}


def _load_criteria_descriptions(user_dir: Path, task_name: str, criteria_names: set[str]) -> dict[str, str]:
    """Load criteria descriptions from the config JSON file that matches this eval."""
    configs_dir = user_dir / "configs"
    if not configs_dir.exists():
        return {}
    for json_file in configs_dir.glob("*.json"):
        try:
            data = json.loads(json_file.read_text())
            criteria = data.get("criteria", [])
            if not criteria:
                continue
            config_names = {c["name"] for c in criteria}
            if criteria_names and criteria_names.issubset(config_names):
                return {c["name"]: c["description"] for c in criteria}
        except Exception:
            continue
    return {}


@router.get("/sample")
async def get_sample_detail(
    log_file: str,
    sample_id: str,
    user_id: str = Depends(_get_user_id),
):
    """Get full detail for a single sample including judge reasoning."""
    log_dir = get_user_log_dir(user_id)
    if not log_file.startswith(log_dir) and f"/users/{user_id}/" not in log_file:
        raise HTTPException(status_code=403, detail="Access denied")

    full_logs = await _read_full_logs([log_file])
    if not full_logs:
        raise HTTPException(status_code=500, detail="Failed to read log")

    log = full_logs[0]
    for sample in log.get("samples", []):
        if str(sample["id"]) == sample_id:
            return {
                "id": sample["id"],
                "model": log["model"],
                "input": sample["input"],
                "target": sample["target"],
                "output": sample.get("output", ""),
                "scores": sample.get("scores", {}),
                "modelUsage": sample.get("model_usage", {}),
            }

    raise HTTPException(status_code=404, detail="Sample not found")
