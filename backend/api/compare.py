"""Comparison API for viewing evaluation results across multiple models.

Uses read_eval_log_async() directly since FastAPI endpoints are already
in an async context — no subprocess or nest_asyncio needed.
"""

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from inspect_ai._view.common import list_eval_logs_async
from inspect_ai.log import read_eval_log_async

from backend.core.pricing import calculate_cost
from backend.core.user_storage import get_user_dir, get_user_log_dir

logger = logging.getLogger(__name__)

router = APIRouter()


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


@router.get("/groups")
async def get_comparison_groups(user_id: str = Depends(_get_user_id)):
    """List evaluation comparison groups for the user."""
    log_dir = get_user_log_dir(user_id)

    logs = await _read_log_headers(log_dir)

    # Group by run_id
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

        groups.append({
            "id": run_id,
            "task": task_name,
            "configName": config_name,
            "created": run_logs[0].get("created", ""),
            "models": models,
            "sampleCount": run_logs[0].get("dataset_samples", 0),
            "status": run_logs[0].get("status", "unknown"),
            "scores": scores_by_model,
        })

    groups.sort(key=lambda g: g["created"], reverse=True)
    return {"groups": groups}


def _load_criteria_descriptions(user_dir: Path, task_name: str, criteria_names: set[str]) -> dict[str, str]:
    """Load criteria descriptions from the config JSON file that matches this eval."""
    configs_dir = user_dir / "configs"
    if not configs_dir.exists():
        return {}
    # Find the config whose criteria names match this eval's criteria
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


@router.get("/detail")
async def get_comparison_detail(group_id: str, user_id: str = Depends(_get_user_id)):
    """Get full comparison data for a specific evaluation group."""
    user_dir = get_user_dir(user_id)
    log_dir = get_user_log_dir(user_id)

    headers = await _read_log_headers(log_dir)

    # Filter to the requested group
    group_logs = [h for h in headers if h.get("run_id") == group_id]
    if not group_logs:
        group_logs = [h for h in headers if h["file"] == group_id]
    if not group_logs:
        raise HTTPException(status_code=404, detail="Group not found")

    # Read full logs with samples
    log_files = [l["file"] for l in group_logs]
    full_logs = await _read_full_logs(log_files)

    if not full_logs:
        raise HTTPException(status_code=500, detail="Failed to read evaluation logs")

    models = [l["model"] for l in full_logs]

    # Align samples by ID across models
    samples_by_id: dict[str, dict] = {}
    criteria_set: set[str] = set()

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
                for scorer_name, score in sample["scores"].items():
                    score_data["passed"] = score["value"] == "C"
                    metadata = score.get("metadata", {})
                    score_data["score"] = metadata.get("jury_score", 1.0 if score["value"] == "C" else 0.0)
                    score_data["explanation"] = score.get("explanation", "")
                    criteria_results = metadata.get("criteria_results", [])
                    score_data["criteriaResults"] = criteria_results
                    for cr in criteria_results:
                        criteria_set.add(cr["name"])

            samples_by_id[sid]["results"][model] = score_data

    # Compute aggregates
    aggregate: dict[str, dict] = {}
    for model in models:
        model_samples = [
            s["results"][model]
            for s in samples_by_id.values()
            if model in s["results"]
        ]
        total = len(model_samples)
        passed = sum(1 for s in model_samples if s["passed"])
        overall = passed / max(total, 1)

        by_criterion: dict[str, float] = {}
        for criterion in criteria_set:
            criterion_passed = 0
            criterion_total = 0
            for s in model_samples:
                for cr in s.get("criteriaResults", []):
                    if cr["name"] == criterion:
                        criterion_total += 1
                        if cr["passed"]:
                            criterion_passed += 1
            by_criterion[criterion] = criterion_passed / max(criterion_total, 1)

        aggregate[model] = {"overall": overall, "byCriterion": by_criterion}

    # Stats from headers: tokens, cost, latency
    stats: dict[str, dict] = {}
    for log in group_logs:
        model = log["model"]
        started = log.get("started_at")
        completed = log.get("completed_at")

        # Calculate latency
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

            # Calculate cost from pricing table
            cost = calculate_cost(model, total_input, total_output)
            stats[model]["cost"] = cost

            # Tokens per second
            if latency_seconds and latency_seconds > 0:
                stats[model]["tokensPerSecond"] = round(total_output / latency_seconds, 1)

    # Load criteria descriptions from config file
    task_name = group_logs[0].get("task", "")
    criteria_descriptions = _load_criteria_descriptions(user_dir, task_name, criteria_set)

    return {
        "groupId": group_id,
        "task": task_name,
        "models": models,
        "criteria": sorted(criteria_set),
        "criteriaDescriptions": criteria_descriptions,
        "aggregate": aggregate,
        "samples": list(samples_by_id.values()),
        "stats": stats,
    }


@router.get("/sample")
async def get_sample_detail(
    log_file: str,
    sample_id: str,
    user_id: str = Depends(_get_user_id),
):
    """Get full detail for a single sample including judge reasoning."""
    # Validate the log file belongs to this user
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
