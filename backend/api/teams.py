"""Teams API — a team is a principal that eval grants can target.

Identity comes only from X-Forwarded-User (same shim as compare.py). All
membership/admin checks are deny-by-default: a caller must be a member to see
a team, and an admin to mutate it. See docs/EVAL_SHARING_DESIGN.md §8/§9.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter()


async def _get_user_id(request: Request) -> str:
    user_id = request.headers.get("X-Forwarded-User")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


def _db():
    from backend.api.main import db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")
    return db


class CreateTeamRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


class AddMemberRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    role: str = Field("member", pattern="^(admin|member)$")


async def _require_member(team_id: str, user_id: str) -> str:
    """Return the caller's role in the team, or 403 if they're not a member."""
    role = await _db().get_team_role(team_id, user_id)
    if role is None:
        logger.warning(f"[TEAM] denied {user_id} -> team {team_id} (not a member)")
        raise HTTPException(status_code=403, detail="Not a team member")
    return role


async def _require_admin(team_id: str, user_id: str) -> None:
    if await _require_member(team_id, user_id) != "admin":
        raise HTTPException(status_code=403, detail="Team admin required")


@router.post("")
async def create_team(req: CreateTeamRequest, user_id: str = Depends(_get_user_id)):
    """Create a team; the caller becomes its first admin member."""
    team_id = uuid.uuid4().hex[:16]
    # Ensure the creator exists as a user (FK target) before team rows.
    await _db().create_user(user_id, user_id)
    await _db().create_team(team_id, req.name, user_id)
    logger.info(f"[TEAM] {user_id} created team {team_id} ({req.name})")
    return {"id": team_id, "name": req.name}


@router.get("")
async def list_my_teams(user_id: str = Depends(_get_user_id)):
    """List teams the caller belongs to (with names + the caller's role)."""
    return {"teams": await _db().list_teams_for_user_detailed(user_id)}


@router.get("/{team_id}/members")
async def list_members(team_id: str, user_id: str = Depends(_get_user_id)):
    """List a team's members. Caller must be a member to view."""
    await _require_member(team_id, user_id)
    return {"members": await _db().list_team_members(team_id)}


@router.post("/{team_id}/members")
async def add_member(
    team_id: str, req: AddMemberRequest, user_id: str = Depends(_get_user_id)
):
    """Add a member to a team. Admin only."""
    await _require_admin(team_id, user_id)
    # The new member must exist as a user row (FK). They may not have logged in
    # yet — create a stub keyed on the id they were invited by.
    await _db().create_user(req.user_id, req.user_id)
    await _db().add_team_member(team_id, req.user_id, req.role)
    logger.info(f"[TEAM] {user_id} added {req.user_id} ({req.role}) to {team_id}")
    return {"ok": True}


@router.delete("/{team_id}/members/{member_id}")
async def remove_member(
    team_id: str, member_id: str, user_id: str = Depends(_get_user_id)
):
    """Remove a member from a team. Admin only (a member may remove themselves
    too — that's a self-service leave)."""
    if member_id != user_id:
        await _require_admin(team_id, user_id)
    else:
        await _require_member(team_id, user_id)
    deleted = await _db().remove_team_member(team_id, member_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Member not found")
    logger.info(f"[TEAM] {user_id} removed {member_id} from {team_id}")
    return {"ok": True}
