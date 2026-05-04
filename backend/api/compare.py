"""Comparison API for viewing evaluation results across multiple models.

Reads pre-computed JSON from S3/disk. The JSON is built once when an eval
completes (see backend.core.eval_results.precompute_eval_results).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.core.eval_results import precompute_eval_results
from backend.core.user_storage import load_eval_detail, load_eval_groups

logger = logging.getLogger(__name__)

router = APIRouter()


async def _get_user_id(request: Request) -> str:
    user_id = request.headers.get("X-Forwarded-User")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


@router.get("/groups")
async def get_comparison_groups(user_id: str = Depends(_get_user_id)):
    """List evaluation comparison groups for the user."""
    data = load_eval_groups(user_id)
    if data:
        return data

    # Fallback: pre-computed JSON doesn't exist yet (first visit or old data).
    # Build it now, then return.
    await precompute_eval_results(user_id)
    data = load_eval_groups(user_id)
    if data:
        return data
    return {"groups": []}


@router.get("/detail")
async def get_comparison_detail(group_id: str, user_id: str = Depends(_get_user_id)):
    """Get full comparison data for a specific evaluation group."""
    data = load_eval_detail(user_id, group_id)
    if data:
        return data

    # Fallback: pre-compute this group (handles old evals without pre-computed JSON)
    await precompute_eval_results(user_id)
    data = load_eval_detail(user_id, group_id)
    if data:
        return data
    raise HTTPException(status_code=404, detail="Group not found")


@router.post("/rebuild")
async def rebuild_results(user_id: str = Depends(_get_user_id)):
    """Re-parse all .eval files and rebuild pre-computed JSON.

    Use this once to migrate existing evals, or to fix corrupted data.
    """
    await precompute_eval_results(user_id)
    data = load_eval_groups(user_id)
    count = len(data["groups"]) if data else 0
    return {"ok": True, "groups_rebuilt": count}


@router.get("/sample")
async def get_sample_detail(
    log_file: str,
    sample_id: str,
    user_id: str = Depends(_get_user_id),
):
    """Get full detail for a single sample including judge reasoning."""
    from backend.core.eval_results import _read_full_logs
    from backend.core.user_storage import get_user_log_dir

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
