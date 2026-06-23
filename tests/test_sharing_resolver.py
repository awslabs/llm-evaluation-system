"""Unit tests for the cross-user eval sharing authorization resolver.

Stack-free: drives backend.core.sharing.can_read / list_shared_scopes against
a fake in-memory db, so it runs under `pytest tests/` with no Postgres. This is
the only automated reach the feature has (CI runs no integration tests), so it
covers the deny-by-default invariants directly.

The companion live-stack test is verify_grant_isolation.py.
"""
from __future__ import annotations

import pytest

from backend.core.sharing import (
    assert_path_within_owner,
    can_read,
    list_shared_scopes,
    resolve_principals,
)


class FakeDB:
    """Minimal stand-in for Database — only the methods the resolver calls.

    grants: list of dicts {ownerId, groupId, role, _principal: (type, id)}.
    The _principal field is what list_grants_for_principals filters on,
    mirroring the real (principal_type, principal_id) match.
    """

    def __init__(self, grants=None, teams=None, fail_teams=False, fail_grants=False):
        self._grants = grants or []
        self._teams = teams or {}
        self._fail_teams = fail_teams
        self._fail_grants = fail_grants

    async def get_teams_for_user(self, user_id):
        if self._fail_teams:
            raise RuntimeError("team store down")
        return self._teams.get(user_id, [])

    async def list_grants_for_principals(self, principals):
        if self._fail_grants:
            raise RuntimeError("grant store down")
        pset = set(principals)
        return [
            {
                "ownerId": g["ownerId"],
                "groupId": g["groupId"],
                "resourceType": g.get("resourceType", "eval"),
                "role": g["role"],
            }
            for g in self._grants
            if g["_principal"] in pset
        ]


def _grant(owner, group, principal, role="viewer", resource_type="eval"):
    return {
        "ownerId": owner, "groupId": group, "role": role,
        "resourceType": resource_type, "_principal": principal,
    }


# --------------------------------------------------------------------------
# Ownership
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_owner_can_read_own():
    db = FakeDB()
    assert await can_read(db, "alice", "alice", "g1") is True


@pytest.mark.asyncio
async def test_stranger_denied_by_default():
    db = FakeDB()
    assert await can_read(db, "bob", "alice", "g1") is False


@pytest.mark.asyncio
async def test_empty_ids_denied():
    db = FakeDB()
    assert await can_read(db, "", "alice", "g1") is False
    assert await can_read(db, "bob", "", "g1") is False


# --------------------------------------------------------------------------
# Per-eval user grant
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_user_grant_specific_group_allows_only_that_group():
    db = FakeDB(grants=[_grant("alice", "g1", ("user", "bob"))])
    assert await can_read(db, "bob", "alice", "g1") is True
    # A different group is NOT covered by a group-specific grant.
    assert await can_read(db, "bob", "alice", "g2") is False


@pytest.mark.asyncio
async def test_grant_to_other_user_does_not_leak():
    # Grant is to carol, not bob.
    db = FakeDB(grants=[_grant("alice", "g1", ("user", "carol"))])
    assert await can_read(db, "bob", "alice", "g1") is False


# --------------------------------------------------------------------------
# Share-all (group_id is None)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_share_all_covers_any_group_including_unknown():
    db = FakeDB(grants=[_grant("alice", None, ("user", "bob"))])
    assert await can_read(db, "bob", "alice", "g1") is True
    assert await can_read(db, "bob", "alice", "g999") is True
    # ...but only for alice's evals, not some other owner.
    assert await can_read(db, "bob", "dave", "g1") is False


# --------------------------------------------------------------------------
# Team grant
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_team_grant_allows_members_only():
    db = FakeDB(
        grants=[_grant("alice", "g1", ("team", "team7"))],
        teams={"bob": ["team7"], "carol": ["team9"]},
    )
    assert await can_read(db, "bob", "alice", "g1") is True    # bob in team7
    assert await can_read(db, "carol", "alice", "g1") is False  # carol not in team7


# --------------------------------------------------------------------------
# Org-wide grant
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_org_grant_allows_everyone():
    db = FakeDB(grants=[_grant("alice", "g1", ("org", None))])
    assert await can_read(db, "anyone", "alice", "g1") is True


# --------------------------------------------------------------------------
# Fail-closed on errors
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_grant_store_error_denies():
    db = FakeDB(grants=[_grant("alice", "g1", ("user", "bob"))], fail_grants=True)
    # Even though a valid grant exists, a store error must fail CLOSED.
    assert await can_read(db, "bob", "alice", "g1") is False


@pytest.mark.asyncio
async def test_team_store_error_still_allows_self_and_explicit_user_grant():
    # Team lookup fails, but a direct user grant must still work (we degrade
    # to user+org principals, not total denial).
    db = FakeDB(grants=[_grant("alice", "g1", ("user", "bob"))], fail_teams=True)
    assert await can_read(db, "bob", "alice", "g1") is True


@pytest.mark.asyncio
async def test_owner_unaffected_by_team_store_error():
    db = FakeDB(fail_teams=True)
    assert await can_read(db, "alice", "alice", "g1") is True


# --------------------------------------------------------------------------
# resolve_principals
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_principals_includes_self_org_and_teams():
    db = FakeDB(teams={"bob": ["t1", "t2"]})
    principals = await resolve_principals(db, "bob")
    assert ("user", "bob") in principals
    assert ("org", None) in principals
    assert ("team", "t1") in principals
    assert ("team", "t2") in principals


# --------------------------------------------------------------------------
# list_shared_scopes excludes own evals
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_shared_scopes_excludes_self():
    db = FakeDB(grants=[
        _grant("alice", "g1", ("user", "bob")),
        _grant("bob", None, ("user", "bob")),   # bob shared with himself somehow
    ])
    scopes = await list_shared_scopes(db, "bob")
    owners = {s["ownerId"] for s in scopes}
    assert "alice" in owners
    assert "bob" not in owners


# --------------------------------------------------------------------------
# Path boundary re-validation
# --------------------------------------------------------------------------

def test_assert_path_within_owner_rejects_traversal(monkeypatch):
    # get_user_log_dir is imported lazily inside the function, so patch it at
    # its source module (eval_mcp.core.user_storage), not on sharing.
    import eval_mcp.core.user_storage as us
    monkeypatch.setattr(us, "get_user_log_dir",
                        lambda uid: f"/data/users/{uid}/logs")
    # In-scope read.
    assert assert_path_within_owner("/data/users/alice/logs/x.eval", "alice") is True
    # Traversal out of alice into bob must be rejected.
    assert assert_path_within_owner(
        "/data/users/alice/logs/../../bob/logs/secret.eval", "alice") is False
    # Sibling-prefix bypass must be rejected.
    assert assert_path_within_owner("/data/users/alice-evil/logs/x.eval", "alice") is False


def test_assert_path_within_owner_denies_invalid_owner(monkeypatch):
    import eval_mcp.core.user_storage as us

    def _raise(uid):
        raise ValueError("invalid user_id")

    monkeypatch.setattr(us, "get_user_log_dir", _raise)
    assert assert_path_within_owner("/data/users/x/logs/a.eval", "../etc") is False


# --------------------------------------------------------------------------
# Multi-resource: resource_type scopes the grant — a grant on one type must
# NOT authorize a read of another type, even with the same owner + id.
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dataset_grant_does_not_authorize_eval_read():
    # Same id string "x1" granted as a DATASET — must not unlock an EVAL read.
    db = FakeDB(grants=[_grant("alice", "x1", ("user", "bob"), resource_type="dataset")])
    assert await can_read(db, "bob", "alice", "x1", resource_type="dataset") is True
    assert await can_read(db, "bob", "alice", "x1", resource_type="eval") is False


@pytest.mark.asyncio
async def test_judge_share_all_scoped_to_judges():
    db = FakeDB(grants=[_grant("alice", None, ("user", "bob"), resource_type="judge")])
    assert await can_read(db, "bob", "alice", "j1", resource_type="judge") is True
    # share-all judges must not leak datasets/evals/optimizations.
    assert await can_read(db, "bob", "alice", "j1", resource_type="dataset") is False
    assert await can_read(db, "bob", "alice", "anything", resource_type="eval") is False


@pytest.mark.asyncio
async def test_legacy_eval_grant_has_no_resource_type_key():
    # Grants written before multi-resource sharing carry no resourceType; the
    # resolver must treat them as 'eval' (backward compat).
    legacy = {"ownerId": "alice", "groupId": "g1", "role": "viewer",
              "_principal": ("user", "bob")}  # no resourceType key
    db = FakeDB(grants=[legacy])
    assert await can_read(db, "bob", "alice", "g1", resource_type="eval") is True
    assert await can_read(db, "bob", "alice", "g1", resource_type="dataset") is False


@pytest.mark.asyncio
async def test_list_shared_scopes_filters_by_resource_type():
    db = FakeDB(grants=[
        _grant("alice", "d1", ("user", "bob"), resource_type="dataset"),
        _grant("alice", "g1", ("user", "bob"), resource_type="eval"),
    ])
    ds = await list_shared_scopes(db, "bob", "dataset")
    assert {s["groupId"] for s in ds} == {"d1"}
    ev = await list_shared_scopes(db, "bob", "eval")
    assert {s["groupId"] for s in ev} == {"g1"}
    allk = await list_shared_scopes(db, "bob")
    assert len(allk) == 2
