"""Comparison API for viewing evaluation results across multiple models.

Reads pre-computed JSON from S3/disk. The JSON is built once when an eval
completes (see backend.core.eval_results.precompute_eval_results).
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from backend.core.eval_results import precompute_eval_results
from backend.core.user_storage import get_user_log_dir, load_eval_detail, load_eval_groups

logger = logging.getLogger(__name__)

router = APIRouter()


async def _get_user_id(request: Request) -> str:
    user_id = request.headers.get("X-Forwarded-User")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


@router.get("/groups")
async def get_comparison_groups(user_id: str = Depends(_get_user_id)):
    """List evaluation comparison groups for the user.

    Serves from pre-computed cache (fast). Merges in running evals
    from log headers so they appear without waiting for completion.
    """
    from backend.core.eval_results import _read_log_headers, _build_groups_from_headers

    # Serve cached completed evals (instant)
    cached = load_eval_groups(user_id)
    cached_groups = cached.get("groups", []) if cached else []
    cached_ids = {g["id"] for g in cached_groups}

    # Find running evals not in cache
    log_dir = get_user_log_dir(user_id)
    headers = await _read_log_headers(log_dir)
    started_headers = [h for h in headers if h.get("status") == "started"]

    if not started_headers:
        if cached_groups:
            return cached
        # No cache and no running — build fresh
        await precompute_eval_results(user_id)
        return load_eval_groups(user_id) or {"groups": []}

    # Build groups from started headers only, merge with cache
    all_data = _build_groups_from_headers(started_headers)
    new_groups = [g for g in all_data.get("groups", []) if g["id"] not in cached_ids]

    merged = new_groups + cached_groups
    merged.sort(key=lambda g: g.get("created", ""), reverse=True)
    return {"groups": merged}


@router.get("/detail")
async def get_comparison_detail(group_id: str, user_id: str = Depends(_get_user_id)):
    """Get full comparison data for a specific evaluation group."""
    from backend.core.eval_results import _read_log_headers, _build_groups_from_headers

    # For running evals, read partial results directly (skip cache)
    log_dir = get_user_log_dir(user_id)
    headers = await _read_log_headers(log_dir)
    group_headers = [h for h in headers if (h.get("run_id") or h["file"]) == group_id]
    if group_headers and any(h.get("status") == "started" for h in group_headers):
        import asyncio
        from functools import partial
        from inspect_ai.log import read_eval_log_sample_summaries

        models = list(dict.fromkeys(h["model"] for h in group_headers))
        total_samples = group_headers[0].get("dataset_samples", 0)
        samples_by_id: dict[str, dict] = {}
        aggregate: dict[str, dict] = {}

        criteria_names: set[str] = set()
        criteria_votes: dict[str, dict[str, list[bool]]] = {}  # model -> criterion -> [passed]

        for h in group_headers:
            model = h["model"]
            try:
                loop = asyncio.get_event_loop()
                summaries = await loop.run_in_executor(None, partial(read_eval_log_sample_summaries, h["file"]))
                completed = [s for s in summaries if s.scores]
                scores_sum = 0.0
                model_criteria_votes: dict[str, list[bool]] = {}

                for s in completed:
                    score_obj = next(iter(s.scores.values())) if s.scores else None
                    if not score_obj:
                        continue
                    val = score_obj.value
                    if val == "C":
                        scores_sum += 1.0
                    elif isinstance(val, (int, float)):
                        scores_sum += float(val)

                    # Extract per-criterion results
                    if score_obj.metadata and "criteria_results" in score_obj.metadata:
                        for cr in score_obj.metadata["criteria_results"]:
                            cname = cr["name"]
                            criteria_names.add(cname)
                            if cname not in model_criteria_votes:
                                model_criteria_votes[cname] = []
                            model_criteria_votes[cname].append(cr["passed"])

                avg = scores_sum / len(completed) if completed else 0
                by_criterion = {}
                for cname, votes in model_criteria_votes.items():
                    by_criterion[cname] = sum(votes) / len(votes) if votes else 0
                aggregate[model] = {"overall": avg, "byCriterion": by_criterion}
                criteria_votes[model] = model_criteria_votes

                for s in completed:
                    sid = str(s.id)
                    if sid not in samples_by_id:
                        sample_input = s.input if isinstance(s.input, str) else str(s.input[0].content if s.input else "")
                        samples_by_id[sid] = {
                            "id": sid,
                            "input": sample_input[:300],
                            "target": s.target[0] if isinstance(s.target, list) else str(s.target or ""),
                            "results": {},
                        }
                    score_obj = next(iter(s.scores.values())) if s.scores else None
                    passed = score_obj.value == "C" if score_obj else False
                    score_num = 1.0 if passed else (float(score_obj.value) if score_obj and isinstance(score_obj.value, (int, float)) else 0.0)
                    criteria_results = []
                    if score_obj and score_obj.metadata and "criteria_results" in score_obj.metadata:
                        criteria_results = [
                            {"name": cr["name"], "passed": cr["passed"], "votes_for": cr.get("votes_for", 0), "total": cr.get("total", 0)}
                            for cr in score_obj.metadata["criteria_results"]
                        ]
                    samples_by_id[sid]["results"][model] = {
                        "passed": passed,
                        "score": score_num,
                        "output": "",
                        "explanation": score_obj.explanation[:200] if score_obj and score_obj.explanation else "",
                        "criteriaResults": criteria_results,
                    }
            except Exception as e:
                logger.warning(f"Failed to read summaries for {model}: {e}")
                aggregate[model] = {"overall": 0, "byCriterion": {}}

        return {
            "models": models,
            "samples": list(samples_by_id.values()),
            "aggregate": aggregate,
            "criteria": sorted(criteria_names),
            "stats": {m: {"total_tokens": 0} for m in models},
            "status": "running",
            "sampleCount": total_samples,
            "completedSamples": len(samples_by_id),
        }

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
    from backend.core.bedrock_client import BedrockClient
    from backend.core.pdf_report import generate_pdf_report

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
    from backend.core.user_storage import _s3_enabled, _get_s3_client, DATA_BUCKET
    from backend.core.user_storage import _get_json_store_dir

    safe_id = group_id.replace("/", "_").replace("\\", "_")
    filename = f"report_{safe_id}.pdf"

    if _s3_enabled():
        key = f"users/{user_id}/store/reports/{filename}"
        _get_s3_client().put_object(
            Bucket=DATA_BUCKET,
            Key=key,
            Body=pdf_bytes,
            ContentType="application/pdf",
        )
    else:
        reports_dir = _get_json_store_dir(user_id, "reports")
        (reports_dir / filename).write_bytes(pdf_bytes)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="eval_report_{safe_id}.pdf"',
        },
    )
