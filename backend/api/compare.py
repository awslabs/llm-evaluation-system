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
from backend.core import sharing
from eval_mcp.core.eval_results import precompute_eval_results
from eval_mcp.core.user_storage import get_user_log_dir, load_eval_detail, load_eval_groups

logger = logging.getLogger(__name__)

router = APIRouter()


async def _get_user_id(request: Request) -> str:
    user_id = request.headers.get("X-Forwarded-User")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


def _db():
    """The shared Database global (set in main.py lifespan). 503 if absent."""
    from backend.api.main import db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")
    return db


async def _resolve_owner(caller_id: str, group_id: str,
                         owner: Optional[str]) -> str:
    """Return the owner_id to read (group_id) as, after authorizing caller.

    `owner` is the owner hint the SPA carries on each row (from /groups). When
    it's absent or equals the caller, this is a normal own-eval read. When it
    names another user, the caller must hold a grant — deny-by-default via the
    sharing resolver. Raises 403 otherwise. The returned owner_id is what every
    storage read must be scoped to (NEVER trust `owner` without this check).
    """
    if not owner or owner == caller_id:
        return caller_id
    allowed = await sharing.can_read(_db(), caller_id, owner, group_id)
    if not allowed:
        logger.warning(
            f"[ACCESS] denied {caller_id} -> owner={owner} group={group_id}"
        )
        raise HTTPException(status_code=403, detail="Access denied")
    return owner


def _tag_owner(groups_blob: Optional[dict], owner_id: str) -> list:
    """Return the group elements from a user's _groups.json, each tagged with
    its owner so the SPA can badge shared evals and carry the owner hint back
    on /detail. A missing blob yields []."""
    if not groups_blob:
        return []
    out = []
    for g in groups_blob.get("groups", []):
        tagged = dict(g)
        tagged["owner"] = owner_id
        out.append(tagged)
    return out


@router.get("/groups")
async def get_comparison_groups(user_id: str = Depends(_get_user_id)):
    """List evaluation comparison groups visible to the user.

    Returns the caller's own groups plus any shared with them (per-eval, team,
    or org grants), each element tagged with its `owner`. Own evals are served
    from the pre-computed cache (recomputed on a cold miss).
    """
    own = load_eval_groups(user_id)
    if not own:
        await precompute_eval_results(user_id)
        own = load_eval_groups(user_id)
    groups = _tag_owner(own, user_id)

    # Merge in evals shared with the caller. Each scope is (ownerId, groupId,
    # role); groupId=None means all of that owner's evals. We read each owner's
    # precomputed blob and include only the granted groups.
    try:
        scopes = await sharing.list_shared_scopes(_db(), user_id)
    except Exception as e:
        # Sharing is additive — never let a grant-store hiccup hide the user's
        # OWN evals. Log and return just the own set.
        logger.warning(f"[ACCESS] shared-scope listing failed for {user_id}: {e}")
        return {"groups": groups}

    # Group scopes by owner so we load each owner's blob once.
    owners: dict[str, set] = {}
    share_all: set = set()
    for s in scopes:
        owner = s["ownerId"]
        if s["groupId"] is None:
            share_all.add(owner)
        owners.setdefault(owner, set()).add(s["groupId"])

    seen_owners = set()
    for owner in set(list(owners.keys()) + list(share_all)):
        seen_owners.add(owner)
        blob = load_eval_groups(owner)
        if not blob:
            continue
        allow_all = owner in share_all
        allowed_ids = owners.get(owner, set())
        for g in blob.get("groups", []):
            if allow_all or g.get("id") in allowed_ids:
                tagged = dict(g)
                tagged["owner"] = owner
                tagged["shared"] = True
                groups.append(tagged)

    return {"groups": groups}


@router.get("/detail")
async def get_comparison_detail(
    group_id: str,
    owner: Optional[str] = Query(None),
    user_id: str = Depends(_get_user_id),
):
    """Get full comparison data for a specific evaluation group.

    `owner` is the owner hint carried by the SPA from /groups. For a shared
    eval it names another user; the caller must hold a grant (enforced by
    _resolve_owner). All storage reads are scoped to the resolved owner_id.
    """
    owner_id = await _resolve_owner(user_id, group_id, owner)

    data = load_eval_detail(owner_id, group_id)
    if data:
        return data

    # Fallback: compute on demand. Only recompute the caller's OWN evals —
    # a viewer must not trigger writes into the owner's store.
    if owner_id == user_id:
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
    group_id: Optional[str] = Query(None),
    owner: Optional[str] = Query(None),
    user_id: str = Depends(_get_user_id),
):
    """Get full detail for a single sample including judge reasoning.

    `log_file` is a raw caller-supplied path, so this is the sensitive surface
    (the PR #106 boundary). It is gated TWICE: (1) the caller must own the eval
    or hold a grant on (owner, group_id); (2) the resolved owner's log dir is
    the boundary the path must fall within. A grant can therefore never be
    turned into a traversal out of the grantor's own subtree.
    """
    from eval_mcp.core.eval_results import _read_full_logs
    from eval_mcp.core.user_storage import get_user_log_dir

    # (1) Authorize, resolving which owner's data this is. Own path needs no
    # grant; a foreign owner requires an explicit grant (deny-by-default).
    owner_id = await _resolve_owner(user_id, group_id or "", owner)

    # (2) Separator-anchored boundary check against the RESOLVED owner's dir.
    # The previous code only ever checked the caller's own dir; for shared
    # reads we must check the owner's, but only after (1) authorized it.
    if owner_id == user_id:
        in_scope = _is_within_dir(log_file, get_user_log_dir(user_id))
    else:
        in_scope = sharing.assert_path_within_owner(log_file, owner_id)
    if not in_scope:
        logger.warning(
            f"[ACCESS] denied sample read {user_id} -> {log_file} "
            f"(owner={owner_id})"
        )
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
    owner: Optional[str] = Query(None),
    user_id: str = Depends(_get_user_id),
):
    """Generate a PDF report for an evaluation group.

    Combines LLM-generated narrative (neutral analysis) with programmatic
    data tables. Optionally includes chat transcript context for the narrative.

    Args:
        group_id: Evaluation group to report on.
        session_id: Optional chat session ID to pull transcript for context.
        monthly_volume: Projected monthly call volume for cost projections.
        owner: Owner hint for a shared eval; gated by the sharing resolver.
    """
    from eval_mcp.core.bedrock_client import BedrockClient
    from eval_mcp.core.pdf_report import generate_pdf_report

    # Authorize + resolve which owner's eval data to read.
    owner_id = await _resolve_owner(user_id, group_id, owner)

    # Load evaluation data scoped to the resolved owner.
    detail = load_eval_detail(owner_id, group_id)
    if not detail and owner_id == user_id:
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


# ---------------------------------------------------------------------------
# Share management. A user manages grants on THEIR OWN evals only — owner_id is
# always the authenticated caller, never client-supplied, so a caller can't
# create or revoke grants on someone else's data.
# ---------------------------------------------------------------------------

from pydantic import BaseModel, Field  # noqa: E402


class ShareRequest(BaseModel):
    principal_type: str = Field(..., pattern="^(user|team|org)$")
    principal_id: Optional[str] = None   # required for user/team, ignored for org
    group_id: Optional[str] = None       # None = share ALL of caller's evals
    role: str = Field("viewer", pattern="^(viewer|editor|owner)$")


@router.post("/shares")
async def create_share(req: ShareRequest, user_id: str = Depends(_get_user_id)):
    """Grant another user/team/org read access to the caller's eval(s).

    owner_id is the authenticated caller. group_id=None shares ALL the caller's
    evals (including future ones) — the UI must surface this explicitly.
    """
    if req.principal_type in ("user", "team") and not req.principal_id:
        raise HTTPException(
            status_code=400,
            detail=f"principal_id required for principal_type={req.principal_type}",
        )
    principal_id = None if req.principal_type == "org" else req.principal_id
    # Only read-sharing is supported in v1; reject editor/owner until the write
    # paths are gated (deny-by-default rather than silently granting more).
    if req.role != "viewer":
        raise HTTPException(
            status_code=400,
            detail="only role=viewer is supported in this version",
        )
    grant_id = await _db().add_grant(
        owner_id=user_id,
        group_id=req.group_id,
        principal_type=req.principal_type,
        principal_id=principal_id,
        granted_by=user_id,
        role=req.role,
    )
    return {"id": grant_id, "ok": True}


@router.get("/shares")
async def list_my_shares(user_id: str = Depends(_get_user_id)):
    """List grants the caller has created on their own evals."""
    return {"shares": await _db().list_grants_by_owner(user_id)}


@router.get("/users/search")
async def search_users(
    q: str = Query(..., min_length=1),
    user_id: str = Depends(_get_user_id),
):
    """Find users to share with, by id or email substring. Auth-gated like
    every route; returns only {id, email} (no other PII). Grants key on id."""
    return {"users": await _db().search_users(q, limit=10)}


@router.delete("/shares/{grant_id}")
async def revoke_share(grant_id: str, user_id: str = Depends(_get_user_id)):
    """Revoke a grant. Scoped to the caller's own grants (owner_id check in
    the query), so a caller can't revoke grants on another user's evals."""
    deleted = await _db().remove_grant(grant_id, owner_id=user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Grant not found")
    return {"ok": True}
