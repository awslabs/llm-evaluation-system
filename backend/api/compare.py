"""Comparison API for viewing evaluation results across multiple models.

Reads pre-computed JSON from S3/disk. The JSON is built once when an eval
completes (see backend.core.eval_results.precompute_eval_results).

Live progress for in-flight evaluations is served by /api/compare/progress,
not by these endpoints.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from backend.core.inspect_viewer import _is_within_dir
from eval_mcp.core.eval_results import precompute_eval_results
from eval_mcp.core.user_storage import get_user_log_dir, load_eval_detail, load_eval_groups

logger = logging.getLogger(__name__)

router = APIRouter()


async def _get_user_id(request: Request) -> str:
    user_id = request.headers.get("X-Forwarded-User")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


@router.get("/groups")
async def get_comparison_groups(user_id: str = Depends(_get_user_id)):
    """List evaluation comparison groups for the user, served from the pre-computed cache."""
    cached = load_eval_groups(user_id)
    if cached:
        return cached
    await precompute_eval_results(user_id)
    return load_eval_groups(user_id) or {"groups": []}


@router.get("/detail")
async def get_comparison_detail(group_id: str, user_id: str = Depends(_get_user_id)):
    """Get full comparison data for a specific evaluation group."""
    data = load_eval_detail(user_id, group_id)
    if data:
        return data

    # Fallback: compute this group on demand
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
    await precompute_eval_results(user_id, force=True)
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
    from eval_mcp.core.eval_results import _read_full_logs
    from eval_mcp.core.user_storage import get_user_log_dir

    # Real path-boundary check (normalized, separator-anchored) against the
    # caller's own log dir. The previous `startswith OR "/users/{uid}/" in path`
    # was a substring test and bypassable (a path merely CONTAINING the segment
    # passed). get_user_log_dir already includes the user_id, so this confines
    # reads to {.../users/{user_id}/logs}.
    if not _is_within_dir(log_file, get_user_log_dir(user_id)):
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


@router.get("/progress")
async def get_eval_progress(user_id: str = Depends(_get_user_id)):
    """Get progress of in-progress evaluations.

    Reads the shared log buffer written by --log-shared to show
    partial results while evaluations are still running.
    """
    from inspect_ai._view.common import list_eval_logs_async
    from inspect_ai.log import read_eval_log_async
    from inspect_ai.log._recorders.buffer.filestore import SampleBufferFilestore

    log_dir = get_user_log_dir(user_id)

    try:
        all_logs = await list_eval_logs_async(log_dir)
    except Exception:
        return {"running": False, "evals": []}

    running_evals = []
    for log_info in all_logs:
        try:
            log = await read_eval_log_async(log_info.name, header_only=True)
            if log.status != "started":
                continue

            total_samples = log.eval.dataset.samples if log.eval.dataset else 0

            # Try reading shared buffer for completed sample count
            completed = 0
            try:
                filestore = SampleBufferFilestore(log_info.name, create=False)
                manifest = filestore.read_manifest()
                if manifest:
                    completed = manifest.total_samples
            except Exception:
                pass

            running_evals.append({
                "model": log.eval.model,
                "status": "running",
                "total_samples": total_samples,
                "completed_samples": completed,
                "progress_pct": round(completed / total_samples * 100) if total_samples > 0 else 0,
                "run_id": log.eval.run_id,
                "started": log.eval.created,
            })
        except Exception:
            continue

    return {
        "running": len(running_evals) > 0,
        "evals": running_evals,
    }


@router.get("/report/pdf")
async def generate_report_pdf(
    group_id: str,
    session_id: Optional[str] = Query(None),
    monthly_volume: int = Query(10000, ge=100, le=10_000_000),
    user_id: str = Depends(_get_user_id),
):
    """Generate a PDF report for an evaluation group.

    Combines LLM-generated narrative (neutral analysis) with programmatic
    data tables. Optionally includes chat transcript context for the narrative.

    Args:
        group_id: Evaluation group to report on.
        session_id: Optional chat session ID to pull transcript for context.
        monthly_volume: Projected monthly call volume for cost projections.
    """
    from eval_mcp.core.bedrock_client import BedrockClient
    from eval_mcp.core.pdf_report import generate_pdf_report

    # Load evaluation data
    detail = load_eval_detail(user_id, group_id)
    if not detail:
        await precompute_eval_results(user_id)
        detail = load_eval_detail(user_id, group_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Evaluation group not found")

    # Load chat transcript if session_id provided
    transcript = None
    if session_id:
        try:
            from backend.api.main import db
            if db:
                messages = await db.get_session_messages(session_id)
                transcript = messages
        except Exception as e:
            logger.warning(f"Failed to load transcript for session {session_id}: {e}")

    # Generate PDF
    bedrock = BedrockClient()
    pdf_bytes = await generate_pdf_report(
        detail=detail,
        bedrock=bedrock,
        transcript=transcript,
        monthly_volume=monthly_volume,
    )

    # Store the PDF for later access
    import os
    from eval_mcp.core.user_storage import _s3_enabled, _get_s3_client, DATA_BUCKET, get_user_base_dir

    safe_id = group_id.replace("/", "_").replace("\\", "_")
    filename = f"report_{safe_id}.pdf"

    if _s3_enabled():
        key = f"users/{user_id}/reports/{filename}"
        _get_s3_client().put_object(
            Bucket=DATA_BUCKET,
            Key=key,
            Body=pdf_bytes,
            ContentType="application/pdf",
        )
    else:
        if not user_id or '/' in user_id or '\\' in user_id or user_id in ('.', '..'):
            raise ValueError(f"invalid user_id: {user_id!r}")
        base_real = os.path.realpath(str(get_user_base_dir()))
        pdf_real = os.path.realpath(os.path.join(base_real, user_id, "reports", filename))
        if not pdf_real.startswith(base_real + os.sep):
            raise ValueError(f"path escape attempt: {pdf_real}")
        os.makedirs(os.path.dirname(pdf_real), exist_ok=True)
        with open(pdf_real, "wb") as f:
            f.write(pdf_bytes)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="eval_report_{safe_id}.pdf"',
        },
    )


@router.get("/report/{group_id}")
async def download_report(group_id: str, user_id: str = Depends(_get_user_id)):
    """Serve a previously generated PDF report for an evaluation group.

    Reads from S3 in production, local disk in dev. Returns 404 if the
    report hasn't been generated yet (in which case the caller should POST
    to /report/pdf or ask the MCP agent to generate one).
    """
    import os
    from eval_mcp.core.user_storage import (
        DATA_BUCKET,
        _get_s3_client,
        _s3_enabled,
        get_user_base_dir,
    )

    if not user_id or "/" in user_id or "\\" in user_id or user_id in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid user_id")
    safe_id = group_id.replace("/", "_").replace("\\", "_")
    filename = f"report_{safe_id}.pdf"

    if _s3_enabled():
        key = f"users/{user_id}/reports/{filename}"
        try:
            obj = _get_s3_client().get_object(Bucket=DATA_BUCKET, Key=key)
        except Exception as e:
            if getattr(e, "response", {}).get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise HTTPException(
                    status_code=404,
                    detail="Report not generated yet.",
                )
            logger.warning(f"Failed to fetch report s3://{DATA_BUCKET}/{key}: {e}")
            raise HTTPException(status_code=500, detail="failed to fetch report")
        pdf_bytes = obj["Body"].read()
    else:
        base_real = os.path.realpath(str(get_user_base_dir()))
        pdf_real = os.path.realpath(os.path.join(base_real, user_id, "reports", filename))
        if not pdf_real.startswith(base_real + os.sep):
            raise HTTPException(status_code=400, detail="invalid path")
        if not os.path.isfile(pdf_real):
            raise HTTPException(status_code=404, detail="Report not generated yet.")
        with open(pdf_real, "rb") as f:
            pdf_bytes = f.read()

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="eval_report_{safe_id}.pdf"',
        },
    )
