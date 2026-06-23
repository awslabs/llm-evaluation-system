"""Unit tests for grant-aware Inspect viewer policies (the PR #106 surface).

Stack-free: a fake Request carries X-Forwarded-User, and _has_grant is
monkeypatched so we test the policy LOGIC (own-scope, grant-scope, read-only,
map pass-through) without a DB. The boundary primitives (_normalize_key /
_is_within_dir) run for real, so traversal/sibling-prefix defenses are exercised.
"""
from __future__ import annotations

import pytest

import backend.core.inspect_viewer as iv

ROOT = "/data/users"


class FakeRequest:
    def __init__(self, user):
        self.headers = {"X-Forwarded-User": user} if user else {}


def _patch_grants(monkeypatch, allowed):
    """allowed: set of (caller, owner, group_id) tuples that should pass."""
    async def fake_has_grant(caller, owner, group_id):
        return (caller, owner, group_id) in allowed or (caller, owner, None) in allowed
    monkeypatch.setattr(iv, "_has_grant", fake_has_grant)
    # run_id resolution is I/O; pin it deterministically.
    async def fake_run_id(file):
        return "g1" if "g1" in file else None
    monkeypatch.setattr(iv, "_run_id_of", fake_run_id)


# --------------------------------------------------------------------------
# can_read
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_can_read_own_subtree(monkeypatch):
    _patch_grants(monkeypatch, set())
    pol = iv.UserAccessPolicy(ROOT)
    assert await pol.can_read(FakeRequest("alice"), f"{ROOT}/alice/logs/g1.eval") is True


@pytest.mark.asyncio
async def test_can_read_foreign_denied_without_grant(monkeypatch):
    _patch_grants(monkeypatch, set())
    pol = iv.UserAccessPolicy(ROOT)
    assert await pol.can_read(FakeRequest("bob"), f"{ROOT}/alice/logs/g1.eval") is False


@pytest.mark.asyncio
async def test_can_read_foreign_allowed_with_grant(monkeypatch):
    _patch_grants(monkeypatch, {("bob", "alice", "g1")})
    pol = iv.UserAccessPolicy(ROOT)
    assert await pol.can_read(FakeRequest("bob"), f"{ROOT}/alice/logs/g1.eval") is True


@pytest.mark.asyncio
async def test_can_read_traversal_resolves_to_real_owner(monkeypatch):
    # A path that traverses into alice must be grant-checked against ALICE,
    # not whatever prefix it started with. With no grant on alice → denied.
    _patch_grants(monkeypatch, set())
    pol = iv.UserAccessPolicy(ROOT)
    sneaky = f"{ROOT}/bob/logs/../../alice/logs/g1.eval"
    assert await pol.can_read(FakeRequest("bob"), sneaky) is False


# --------------------------------------------------------------------------
# Read-only: delete/write stay self-only even WITH a read grant
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_can_delete_self_only_despite_grant(monkeypatch):
    _patch_grants(monkeypatch, {("bob", "alice", "g1")})  # bob may READ alice
    pol = iv.UserAccessPolicy(ROOT)
    f = f"{ROOT}/alice/logs/g1.eval"
    assert await pol.can_read(FakeRequest("bob"), f) is True       # read: yes
    assert await pol.can_delete(FakeRequest("bob"), f) is False    # delete: no
    assert await pol.can_write(FakeRequest("bob"), f) is False     # write: no


# --------------------------------------------------------------------------
# can_list
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_can_list_root_allowed(monkeypatch):
    _patch_grants(monkeypatch, set())
    pol = iv.UserAccessPolicy(ROOT)
    assert await pol.can_list(FakeRequest("alice"), ROOT) is True


@pytest.mark.asyncio
async def test_can_list_foreign_needs_owner_level_grant(monkeypatch):
    # A per-group grant does NOT open the whole dir (listing has no run_id);
    # only a share-all/team/org (group_id=None) grant authorizes a dir listing.
    _patch_grants(monkeypatch, {("bob", "alice", "g1")})  # group-specific only
    pol = iv.UserAccessPolicy(ROOT)
    assert await pol.can_list(FakeRequest("bob"), f"{ROOT}/alice/logs") is False

    _patch_grants(monkeypatch, {("bob", "alice", None)})  # share-all
    assert await pol.can_list(FakeRequest("bob"), f"{ROOT}/alice/logs") is True


# --------------------------------------------------------------------------
# Mapping policy: granted paths pass through; ungranted get rewritten
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_map_passes_through_granted_path(monkeypatch):
    _patch_grants(monkeypatch, {("bob", "alice", "g1")})
    pol = iv.UserFileMappingPolicy(ROOT)
    f = f"{ROOT}/alice/logs/g1.eval"
    # Must NOT be rewritten to bob's dir — else the authorized read breaks.
    assert await pol.map(FakeRequest("bob"), f) == f


@pytest.mark.asyncio
async def test_map_rewrites_ungranted_foreign_path(monkeypatch):
    _patch_grants(monkeypatch, set())
    pol = iv.UserFileMappingPolicy(ROOT)
    f = f"{ROOT}/alice/logs/g1.eval"
    # No grant → rewritten into bob's own logs dir (can't read alice's).
    assert await pol.map(FakeRequest("bob"), f) == f"{ROOT}/bob/logs"


@pytest.mark.asyncio
async def test_map_own_path_unchanged(monkeypatch):
    _patch_grants(monkeypatch, set())
    pol = iv.UserFileMappingPolicy(ROOT)
    f = f"{ROOT}/bob/logs/g1.eval"
    assert await pol.map(FakeRequest("bob"), f) == f
