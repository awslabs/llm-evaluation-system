"""HTTP API for the Prompts Optimized tab.

Thin wrapper over the persistence layer added in
``eval_mcp/core/user_storage.py``. The frontend hits these endpoints to
populate the list rail and detail pane — both pages mirror the eval
``/api/compare/{groups,detail}`` shape so the page-level state
management stays consistent.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from backend.api.sharing_routes import (
    get_user_id as _get_user_id,
    make_share_router,
    resolve_owner,
)
from backend.core import sharing
from backend.core.sharing_cascade import cascade_optimization
from eval_mcp.core.user_storage import (
    get_optimization_from_db,
    list_optimizations_from_db,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/list")
async def list_optimizations(
    search: str = "",
    user_id: str = Depends(_get_user_id),
):
    """Return optimization-run summary rows, newest-first — the caller's own
    plus any shared with them, each tagged with its owner.

    Each entry is a compact summary (id, dataset, judge, providers,
    winner_iter, winner_test_score, status, created_at). Full details
    come from /detail.
    """
    rows = list_optimizations_from_db(user_id, search_term=search)

    # Merge in optimizations shared with the caller.
    from backend.api.sharing_routes import db as _dbf
    try:
        scopes = await sharing.list_shared_scopes(_dbf(), user_id, "optimization")
    except Exception as e:
        logger.warning(f"[ACCESS] shared optimization scopes failed for {user_id}: {e}")
        scopes = []
    owners: dict = {}
    share_all: set = set()
    for s in scopes:
        if s["groupId"] is None:
            share_all.add(s["ownerId"])
        owners.setdefault(s["ownerId"], set()).add(s["groupId"])
    for owner in set(list(owners) + list(share_all)):
        try:
            owned = list_optimizations_from_db(owner, search_term=search)
        except Exception:
            continue
        allow_all = owner in share_all
        allowed_ids = owners.get(owner, set())
        for row in owned:
            if allow_all or row.get("id") in allowed_ids:
                tagged = dict(row)
                tagged["owner"] = owner
                tagged["shared"] = True
                rows.append(tagged)

    return {"optimizations": rows}


@router.get("/detail")
async def get_optimization_detail(
    id: str,
    owner: Optional[str] = Query(None),
    user_id: str = Depends(_get_user_id),
):
    """Return the full optimization record by ID.

    `owner` is the hint carried from /list for a shared record; the caller must
    hold a grant (enforced by resolve_owner). Reads are scoped to the resolved
    owner. Includes per-iteration history, test scores, rationales, metadata.
    """
    owner_id = await resolve_owner(user_id, id, owner, "optimization")
    record = get_optimization_from_db(owner_id, id)
    if not record:
        raise HTTPException(status_code=404, detail="Optimization not found")
    return record


# Share management for optimizations, with cascade to the referenced
# dataset/judge/eval logs so a shared optimization's drill-ins resolve.
router.include_router(make_share_router("optimization", cascade=cascade_optimization))
