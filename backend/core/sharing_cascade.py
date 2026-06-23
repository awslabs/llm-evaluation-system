"""Cascade helpers — when a resource that references others is shared, grant
the referenced resources too so the viewer's drill-ins resolve.

Kept separate from sharing.py (the pure resolver) because cascade needs the
storage layer to look up what a resource references. Each cascade fn:
  (db, owner_id, resource_id, principal_type, principal_id) -> list[str]
returns short labels of what it additionally granted, for the API response.

All grants are scoped to owner_id (the sharer) — cascade can only ever share
the OWNER's own referenced resources, never escalate to a third party's.
"""

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


async def cascade_optimization(
    db, owner_id: str, optimization_id: str,
    principal_type: str, principal_id: Optional[str],
) -> List[str]:
    """Sharing an optimization → also grant its dataset, judge, and the eval
    logs from each iteration, all owned by `owner_id`, to the same principal.

    The optimization record stores dataset/judge as NAMES (resolved in the
    owner's namespace) and per-iteration eval_run_ids. We resolve names→ids
    against the owner's store and grant each referenced resource.
    """
    from eval_mcp.core.user_storage import (
        get_optimization_from_db,
        get_dataset_by_name,
        get_judge_by_name,
    )

    record = get_optimization_from_db(owner_id, optimization_id)
    if not record:
        return []

    granted: List[str] = []

    async def _grant(resource_type: str, group_id: str, label: str):
        await db.add_grant(
            owner_id=owner_id, group_id=group_id,
            principal_type=principal_type, principal_id=principal_id,
            granted_by=owner_id, role="viewer", resource_type=resource_type,
        )
        granted.append(label)

    # Dataset (name → id in owner's namespace).
    ds_name = record.get("dataset")
    if ds_name:
        ds = get_dataset_by_name(owner_id, ds_name)
        if ds:
            await _grant("dataset", ds["id"], f"dataset:{ds_name}")

    # Judge (name → id).
    judge_name = record.get("judge")
    if judge_name:
        j = get_judge_by_name(owner_id, judge_name)
        if j:
            await _grant("judge", j["id"], f"judge:{judge_name}")

    # Per-iteration eval logs (run_ids) — so "view the underlying eval" works.
    seen = set()
    for h in record.get("history", []):
        rid = h.get("eval_run_id")
        if rid and rid not in seen:
            seen.add(rid)
            await _grant("eval", rid, f"eval:{rid}")

    if granted:
        logger.info(f"[GRANT] cascade for optimization {optimization_id} "
                    f"(owner={owner_id}) granted: {granted}")
    return granted


async def cascade_eval(
    db, owner_id: str, group_id: str,
    principal_type: str, principal_id: Optional[str],
) -> List[str]:
    """Sharing an eval → also grant the dataset + judge its config referenced,
    so a viewer can inspect them. The eval detail JSON is self-contained for
    SCORES, but the source dataset/judge are separate resources; share them
    too for a complete picture.

    Eval logs don't store the dataset/judge id directly; the eval config does.
    We best-effort resolve via the detail record's task/config metadata. If we
    can't resolve a reference, we skip it (the eval itself is already shared and
    readable on its own).
    """
    from eval_mcp.core.user_storage import load_eval_detail

    detail = load_eval_detail(owner_id, group_id)
    if not detail:
        return []

    granted: List[str] = []
    # Detail may carry dataset/judge names under config metadata. These keys are
    # optional — only cascade when present.
    for key, rtype, resolver_name in (
        ("datasetName", "dataset", "get_dataset_by_name"),
        ("judgeName", "judge", "get_judge_by_name"),
    ):
        name = detail.get(key)
        if not name:
            continue
        import eval_mcp.core.user_storage as us
        resolver = getattr(us, resolver_name)
        ref = resolver(owner_id, name)
        if ref:
            await db.add_grant(
                owner_id=owner_id, group_id=ref["id"],
                principal_type=principal_type, principal_id=principal_id,
                granted_by=owner_id, role="viewer", resource_type=rtype,
            )
            granted.append(f"{rtype}:{name}")

    if granted:
        logger.info(f"[GRANT] cascade for eval {group_id} "
                    f"(owner={owner_id}) granted: {granted}")
    return granted
