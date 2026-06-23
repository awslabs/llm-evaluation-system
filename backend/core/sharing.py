"""Cross-user eval sharing: the authorization resolver.

This is the single policy decision point every read path calls. It is
deny-by-default: it returns access ONLY on an explicit ownership match or an
explicit grant match, and returns None / False otherwise.

It composes the boundary primitives from the PR #106 tenant-isolation fix
rather than reimplementing them:
- identity is the authenticated caller (X-Forwarded-User), never client input;
- the resolved owner is turned into a path via get_user_log_dir() and the
  candidate read path is re-validated with _is_within_dir(), so a grant can
  never be leveraged to read OUTSIDE the grantor's own subtree.

See docs/EVAL_SHARING_DESIGN.md for the full design and threat model.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# A principal is a (type, id) tuple. id is None for the 'org' (everyone)
# principal. These are what a grant can target.
Principal = Tuple[str, Optional[str]]


async def resolve_principals(db, caller_id: str) -> List[Principal]:
    """The set of principals a caller acts as: themselves, every team they
    belong to, and the org-wide 'everyone' principal.

    A grant targeting any of these grants the caller access.
    """
    principals: List[Principal] = [("user", caller_id), ("org", None)]
    try:
        for team_id in await db.get_teams_for_user(caller_id):
            principals.append(("team", team_id))
    except Exception as e:
        # Team lookup failure must not fail OPEN by accident, but it also
        # shouldn't deny a user their OWN evals — we already have ('user',
        # caller) and ('org', None). Log and continue with what we have.
        logger.warning(f"[ACCESS] team resolution failed for {caller_id}: {e}")
    return principals


def _grant_covers(grant: Dict[str, Any], owner_id: str,
                  group_id: Optional[str]) -> bool:
    """True iff a grant row authorizes reading (owner_id, group_id).

    A grant with groupId=None is a share-all over that owner's evals. A grant
    with a groupId matches only that specific group. Both require the owner to
    match — group_id is not globally unique, so owner scopes it.
    """
    if grant["ownerId"] != owner_id:
        return False
    g = grant["groupId"]
    return g is None or g == group_id


async def can_read(db, caller_id: str, owner_id: str,
                   group_id: Optional[str] = None) -> bool:
    """Deny-by-default read check for (owner_id, group_id) by caller_id.

    Returns True only if the caller owns the eval, or a grant explicitly
    authorizes one of the caller's principals to read it. Everything else
    (including any error path) returns False.
    """
    if not caller_id or not owner_id:
        return False

    # 1. Ownership fast path.
    if caller_id == owner_id:
        return True

    # 2. Grant path.
    try:
        principals = await resolve_principals(db, caller_id)
        grants = await db.list_grants_for_principals(principals)
    except Exception as e:
        logger.warning(
            f"[ACCESS] grant lookup failed for {caller_id} -> "
            f"{owner_id}/{group_id}: {e}"
        )
        return False

    for grant in grants:
        if _grant_covers(grant, owner_id, group_id):
            return True

    return False


def assert_path_within_owner(read_path: str, owner_id: str) -> bool:
    """Re-validate that a concrete read path stays within the resolved
    owner's log subtree, using the PR #106 boundary primitive.

    Call this AFTER can_read() returns True, on any code path that touches a
    raw filesystem/S3 path (e.g. /api/compare/sample). It guarantees a grant
    can't be turned into a traversal out of the grantor's own dir. Returns
    False on any escape — callers turn that into a 403.
    """
    # Imported lazily so the pure authz logic (can_read) stays free of the
    # inspect_ai / filesystem dependency chain and is unit-testable stack-free.
    from backend.core.inspect_viewer import _is_within_dir
    from eval_mcp.core.user_storage import get_user_log_dir

    try:
        owner_root = get_user_log_dir(owner_id)
    except ValueError:
        return False
    return _is_within_dir(read_path, owner_root)


async def list_shared_scopes(db, caller_id: str) -> List[Dict[str, Any]]:
    """Return the (ownerId, groupId, role) scopes shared with the caller —
    excluding the caller's own evals. Used by /api/compare/groups to merge a
    'Shared with me' section. groupId=None means all of that owner's evals.
    """
    principals = await resolve_principals(db, caller_id)
    grants = await db.list_grants_for_principals(principals)
    # Drop self-grants (a user sharing with themselves) — own evals are
    # already listed via the normal path.
    return [g for g in grants if g["ownerId"] != caller_id]
