"""Comparison API for viewing evaluation results across multiple models.

Reads .eval log files via subprocess (avoids uvloop conflict with inspect_ai),
groups them by run_id, and returns structured JSON for the comparison frontend.
"""

import asyncio
import json
import logging
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.core.user_storage import get_user_dir

logger = logging.getLogger(__name__)

router = APIRouter()


async def _get_user_id(request: Request) -> str:
    user_id = request.headers.get("X-Forwarded-User")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


async def _read_logs_subprocess(log_dir: Path, header_only: bool = True) -> list[dict]:
    """Read .eval log files via subprocess to avoid uvloop conflict."""
    eval_files = sorted(log_dir.glob("*.eval"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not eval_files:
        return []

    file_list = json.dumps([str(f) for f in eval_files])
    script = f"""
import json, sys
from inspect_ai.log import read_eval_log

files = json.loads('''{file_list}''')
results = []
for f in files:
    try:
        log = read_eval_log(f, header_only={header_only})
        entry = {{
            "file": f,
            "run_id": log.eval.run_id if log.eval.run_id else None,
            "task": log.eval.task,
            "model": log.eval.model,
            "status": log.status,
            "created": log.eval.created,
            "dataset_samples": log.eval.dataset.samples if log.eval.dataset else 0,
        }}
        if log.results and log.results.scores:
            entry["scores"] = []
            for s in log.results.scores:
                entry["scores"].append({{
                    "name": s.name,
                    "metrics": {{n: m.value for n, m in s.metrics.items()}}
                }})
        if log.stats:
            usage = {{}}
            if log.stats.model_usage:
                for model_name, mu in log.stats.model_usage.items():
                    usage[model_name] = {{
                        "input_tokens": mu.input_tokens,
                        "output_tokens": mu.output_tokens,
                        "total_tokens": mu.total_tokens,
                    }}
            entry["model_usage"] = usage
            if log.stats.started_at:
                entry["started_at"] = str(log.stats.started_at)
            if log.stats.completed_at:
                entry["completed_at"] = str(log.stats.completed_at)
        results.append(entry)
    except Exception as e:
        results.append({{"file": f, "error": str(e)}})

print(json.dumps(results))
"""
    proc = await asyncio.create_subprocess_exec(
        "python3", "-c", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.error(f"Log reading subprocess failed: {stderr.decode()[:500]}")
        return []
    try:
        return json.loads(stdout.decode())
    except json.JSONDecodeError:
        logger.error(f"Failed to parse log subprocess output")
        return []


async def _read_full_logs_subprocess(log_files: list[str]) -> list[dict]:
    """Read full .eval logs with samples for detail view."""
    file_list = json.dumps(log_files)
    script = f"""
import json, sys
from inspect_ai.log import read_eval_log

files = json.loads('''{file_list}''')
results = []
for f in files:
    try:
        log = read_eval_log(f, header_only=False)
        entry = {{
            "file": f,
            "model": log.eval.model,
            "status": log.status,
        }}
        samples = []
        if log.samples:
            for s in log.samples:
                sample = {{
                    "id": str(s.id),
                    "input": str(s.input) if isinstance(s.input, str) else str(s.input[0].content if s.input else ""),
                    "target": s.target[0] if isinstance(s.target, list) else str(s.target) if s.target else "",
                    "output": s.output.completion[:500] if s.output else "",
                }}
                if s.scores:
                    sample["scores"] = {{}}
                    for scorer_name, score in s.scores.items():
                        score_data = {{
                            "value": str(score.value),
                            "explanation": score.explanation or "",
                        }}
                        if score.metadata:
                            score_data["metadata"] = score.metadata
                        sample["scores"][scorer_name] = score_data
                if s.model_usage:
                    sample["model_usage"] = {{
                        k: {{"input_tokens": v.input_tokens, "output_tokens": v.output_tokens, "total_tokens": v.total_tokens}}
                        for k, v in s.model_usage.items()
                    }}
                samples.append(sample)
        entry["samples"] = samples
        results.append(entry)
    except Exception as e:
        results.append({{"file": f, "error": str(e)}})

print(json.dumps(results, default=str))
"""
    proc = await asyncio.create_subprocess_exec(
        "python3", "-c", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.error(f"Full log reading failed: {stderr.decode()[:500]}")
        return []
    try:
        return json.loads(stdout.decode())
    except json.JSONDecodeError:
        logger.error(f"Failed to parse full log output")
        return []


@router.get("/groups")
async def get_comparison_groups(user_id: str = Depends(_get_user_id)):
    """List evaluation comparison groups for the user."""
    user_dir = get_user_dir(user_id)
    log_dir = user_dir / "logs"
    if not log_dir.exists():
        return {"groups": []}

    logs = await _read_logs_subprocess(log_dir, header_only=True)
    logs = [l for l in logs if "error" not in l]

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

        # Extract config name from task (our tasks are named after the config)
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

    # Sort by created time, newest first
    groups.sort(key=lambda g: g["created"], reverse=True)
    return {"groups": groups}


@router.get("/detail")
async def get_comparison_detail(group_id: str, user_id: str = Depends(_get_user_id)):
    """Get full comparison data for a specific evaluation group."""
    user_dir = get_user_dir(user_id)
    log_dir = user_dir / "logs"
    if not log_dir.exists():
        raise HTTPException(status_code=404, detail="No logs found")

    # First get headers to find files in this group
    headers = await _read_logs_subprocess(log_dir, header_only=True)
    headers = [h for h in headers if "error" not in h]

    # Filter to the requested group
    group_logs = [h for h in headers if h.get("run_id") == group_id]
    if not group_logs:
        # Fallback: try matching by file path
        group_logs = [h for h in headers if h["file"] == group_id]
    if not group_logs:
        raise HTTPException(status_code=404, detail="Group not found")

    # Read full logs with samples
    log_files = [l["file"] for l in group_logs]
    full_logs = await _read_full_logs_subprocess(log_files)
    full_logs = [l for l in full_logs if "error" not in l]

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
                    "input": sample["input"][:300],
                    "target": sample["target"][:300],
                    "results": {},
                }

            # Extract score data
            score_data = {"passed": False, "score": 0.0, "output": sample.get("output", "")[:300]}
            if sample.get("scores"):
                for scorer_name, score in sample["scores"].items():
                    score_data["passed"] = score["value"] == "C"
                    metadata = score.get("metadata", {})
                    score_data["score"] = metadata.get("jury_score", 1.0 if score["value"] == "C" else 0.0)
                    score_data["explanation"] = score.get("explanation", "")

                    # Extract criteria results
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

    # Stats from headers
    stats: dict[str, dict] = {}
    for log in group_logs:
        model = log["model"]
        stats[model] = {
            "startedAt": log.get("started_at"),
            "completedAt": log.get("completed_at"),
        }
        if log.get("model_usage"):
            total_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            for usage in log["model_usage"].values():
                total_usage["input_tokens"] += usage.get("input_tokens", 0)
                total_usage["output_tokens"] += usage.get("output_tokens", 0)
                total_usage["total_tokens"] += usage.get("total_tokens", 0)
            stats[model].update(total_usage)

    return {
        "groupId": group_id,
        "task": group_logs[0].get("task", ""),
        "models": models,
        "criteria": sorted(criteria_set),
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
    user_dir = get_user_dir(user_id)

    # Security: ensure the log file is within user's directory
    log_path = Path(log_file)
    if not str(log_path).startswith(str(user_dir)):
        raise HTTPException(status_code=403, detail="Access denied")
    if not log_path.exists():
        raise HTTPException(status_code=404, detail="Log file not found")

    full_logs = await _read_full_logs_subprocess([str(log_path)])
    if not full_logs or "error" in full_logs[0]:
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
