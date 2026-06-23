"""Shared helpers + a reusable share-management router for cross-user sharing.

Eval-result sharing (the first surface) lives inline in compare.py. This module
generalizes the same pattern to the other resource types — datasets, judges,
optimizations, documents — so each read route can authorize a cross-user read
with one call, and each resource gets identical share create/list/revoke
endpoints without copy-pasting.

Identity is always the X-Forwarded-User header (never client input); owner is
only ever a *hint* that must be re-authorized via the resolver. See
docs/EVAL_SHARING_DESIGN.md.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.core import sharing

logger = logging.getLogger(__name__)

# Resource types that can be shared. Kept in sync with the DB CHECK-free
# resource_type column; the resolver treats each as an independent namespace.
RESOURCE_TYPES = ("eval", "dataset", "judge", "optimization", "document")


async def get_user_id(request: Request) -> str:
    user_id = request.headers.get("X-Forwarded-User")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


def db():
    """The shared Database global (set in main.py lifespan). 503 if absent."""
    from backend.api.main import db as _db
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")
    return _db


async def resolve_owner(caller_id: str, resource_id: str,
                        owner: Optional[str], resource_type: str) -> str:
    """Return the owner_id to read `resource_id` as, after authorizing caller.

    `owner` is the hint the SPA carries on each row. Absent/self → own read.
    A foreign owner requires a grant (deny-by-default). Raises 403 otherwise.
    The returned owner_id is what every storage read must be scoped to —
    NEVER trust `owner` without this check.
    """
    if not owner or owner == caller_id:
        return caller_id
    allowed = await sharing.can_read(
        db(), caller_id, owner, resource_id, resource_type=resource_type
    )
    if not allowed:
        logger.warning(
            f"[ACCESS] denied {caller_id} -> {resource_type}:{owner}/{resource_id}"
        )
        raise HTTPException(status_code=403, detail="Access denied")
    return owner


class ShareRequest(BaseModel):
    principal_type: str = Field(..., pattern="^(user|team|org)$")
    principal_id: Optional[str] = None    # required for user/team, ignored for org
    resource_id: Optional[str] = None     # None = share ALL of this type
    role: str = Field("viewer", pattern="^(viewer|editor|owner)$")


def make_share_router(resource_type: str, cascade=None) -> APIRouter:
    """Build a router with POST/GET/DELETE /shares for one resource type.

    `cascade`, if given, is an async fn (db, owner_id, resource_id) called after
    a successful share to also grant referenced resources (e.g. an
    optimization's dataset/judge/eval logs). owner_id is always the caller.
    """
    r = APIRouter()

    @r.post("/shares")
    async def create_share(req: ShareRequest, user_id: str = Depends(get_user_id)):
        if req.principal_type in ("user", "team") and not req.principal_id:
            raise HTTPException(
                status_code=400,
                detail=f"principal_id required for principal_type={req.principal_type}",
            )
        if req.role != "viewer":
            raise HTTPException(
                status_code=400,
                detail="only role=viewer is supported in this version",
            )
        principal_id = None if req.principal_type == "org" else req.principal_id
        grant_id = await db().add_grant(
            owner_id=user_id,
            group_id=req.resource_id,
            principal_type=req.principal_type,
            principal_id=principal_id,
            granted_by=user_id,
            role=req.role,
            resource_type=resource_type,
        )
        cascaded = []
        if cascade and req.resource_id:
            try:
                cascaded = await cascade(
                    db(), user_id, req.resource_id, req.principal_type, principal_id
                )
            except Exception as e:
                logger.warning(f"[GRANT] cascade failed for {resource_type} "
                               f"{req.resource_id}: {e}")
        return {"id": grant_id, "ok": True, "cascaded": cascaded}

    @r.get("/shares")
    async def list_shares(user_id: str = Depends(get_user_id)):
        all_owned = await db().list_grants_by_owner(user_id)
        return {"shares": [g for g in all_owned
                           if g.get("resourceType", "eval") == resource_type]}

    @r.delete("/shares/{grant_id}")
    async def revoke_share(grant_id: str, user_id: str = Depends(get_user_id)):
        deleted = await db().remove_grant(grant_id, owner_id=user_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Grant not found")
        return {"ok": True}

    return r
