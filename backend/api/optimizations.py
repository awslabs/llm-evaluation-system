"""HTTP API for the Prompts Optimized tab.

Thin wrapper over the persistence layer added in
``eval_mcp/core/user_storage.py``. The frontend hits these endpoints to
populate the list rail and detail pane — both pages mirror the eval
``/api/compare/{groups,detail}`` shape so the page-level state
management stays consistent.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from eval_mcp.core.user_storage import (
    get_optimization_from_db,
    list_optimizations_from_db,
)

logger = logging.getLogger(__name__)

router = APIRouter()


async def _get_user_id(request: Request) -> str:
    """Same auth shim used by /api/compare — read the cognito proxy header."""
    user_id = request.headers.get("X-Forwarded-User")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


@router.get("/list")
async def list_optimizations(
    search: str = "",
    user_id: str = Depends(_get_user_id),
):
    """Return optimization-run summary rows, newest-first.

    Each entry is a compact summary (id, dataset, judge, providers,
    winner_iter, winner_test_score, status, created_at). Full details
    come from /detail.
    """
    rows = list_optimizations_from_db(user_id, search_term=search)
    return {"optimizations": rows}


@router.get("/detail")
async def get_optimization_detail(
    id: str,
    user_id: str = Depends(_get_user_id),
):
    """Return the full optimization record by ID.

    Includes per-iteration history (prompt text + train pass rate),
    test scores per iter, rationales, and metadata. The frontend
    renders the chart and prompt diff from this single payload.
    """
    record = get_optimization_from_db(user_id, id)
    if not record:
        raise HTTPException(status_code=404, detail="Optimization not found")
    return record
