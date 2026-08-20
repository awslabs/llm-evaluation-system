"""Tests for external/Mantle provider discovery + filtering."""

from unittest.mock import patch

import pytest

from eval_mcp.tools import external_providers as ep


def _enabled(config):
    """Treat every provider as enabled regardless of keys/AWS creds."""
    return True


# Bound before the autouse fixture patches the name, so the catalog tests below
# can exercise the real implementation while the filtering tests stay offline.
_real_list_mantle_models = ep.list_mantle_models


@pytest.fixture(autouse=True)
def _offline_mantle():
    """Force the static-fallback path for every test in this module.

    ``get_external_models`` now prefers the *live* Mantle catalog, so without
    this the whole file would make network calls and its assertions would depend
    on which models AWS happens to serve in the caller's region today. Tests
    that specifically care about the live path patch in their own fixture data.
    """
    with patch.object(ep, "list_mantle_models", lambda *a, **k: None):
        yield


# ---------------------------------------------------------------------------
# Provider filtering / aliasing
# ---------------------------------------------------------------------------


def test_openai_filter_includes_mantle_gpt5():
    """Filtering provider='openai' must surface the Bedrock Mantle GPT-5.x models.

    They live under the 'bedrock-mantle' provider but ARE OpenAI models — the
    match_aliases mechanism makes provider='openai' return them. This is the bug
    that made the chat agent say "GPT-5.4 not available": it filtered to
    provider=openai and missed the Mantle models."""
    with patch.object(ep, "_provider_enabled", _enabled):
        ids = [m["id"] for m in ep.get_external_models("openai")]
    assert "openai/bedrock/gpt-5.4" in ids
    assert "openai/bedrock/gpt-5.5" in ids


def test_bedrock_mantle_canonical_name_still_works():
    """The canonical provider name still returns its models."""
    with patch.object(ep, "_provider_enabled", _enabled):
        ids = [m["id"] for m in ep.get_external_models("bedrock-mantle")]
    assert "openai/bedrock/gpt-5.4" in ids


def test_provider_all_returns_everything():
    with patch.object(ep, "_provider_enabled", _enabled):
        ids = [m["id"] for m in ep.get_external_models("all")]
    assert "openai/bedrock/gpt-5.4" in ids
    assert any(m_id.startswith("openai/gpt-") for m_id in ids)  # direct-API openai too


def test_unrelated_filter_excludes_mantle():
    """A non-matching provider filter must NOT pull in Mantle models."""
    with patch.object(ep, "_provider_enabled", _enabled):
        ids = [m["id"] for m in ep.get_external_models("google")]
    assert not any("bedrock/gpt-5" in m_id for m_id in ids)


# ---------------------------------------------------------------------------
# Static fallback list
# ---------------------------------------------------------------------------


def test_static_fallback_is_mantle_exclusives_only():
    """The offline fallback lists ONLY Mantle-exclusive models.

    Policy: bedrock-runtime is the default path. The GPT-5.6 family has
    runtime CRIS profiles and is surfaced by list_bedrock_models, so listing
    it here would reintroduce duplicate competing ids for the same weights.
    The fallback exists so a user whose Mantle catalog is unreachable still
    sees the models that ONLY Mantle serves (GPT-5.4/5.5, Daybreak).
    """
    ids = {m["id"] for m in ep.EXTERNAL_PROVIDERS["bedrock-mantle"]["models"]}
    assert {
        "openai/bedrock/gpt-5.5",
        "openai/bedrock/gpt-5.4",
    } <= ids
    assert not any("gpt-5.6" in i for i in ids)


# ---------------------------------------------------------------------------
# Live Mantle catalog parsing
# ---------------------------------------------------------------------------


def _mantle_payload():
    """A trimmed real /v1/models response (us-east-2, 2026-07-27)."""
    return {
        "data": [
            {"id": "openai.gpt-5.6-sol", "status": "available"},
            {"id": "openai.gpt-5.6-luna", "status": "available"},
            {"id": "openai.gpt-5.5", "status": "available"},
            {"id": "openai.gpt-5.5-2026-04-23", "status": "available"},
            # Not yet enabled in this region — must be filtered out.
            {"id": "openai.gpt-5.9-unreleased", "status": "unavailable"},
            # Non-OpenAI models share the endpoint but aren't reachable via
            # Inspect's openai/bedrock/ prefix.
            {"id": "anthropic.claude-opus-5", "status": "available"},
            {"id": "qwen.qwen3-32b", "status": "available"},
        ]
    }


def _patch_catalog(payload):
    """Patch the HTTP + token layer that list_mantle_models uses."""

    class _Response:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    return patch.dict(
        "sys.modules",
        {
            "httpx": type("httpx", (), {"get": staticmethod(lambda *a, **k: _Response())}),
            "aws_bedrock_token_generator": type(
                "tokgen", (), {"provide_token": staticmethod(lambda **k: "tok")}
            ),
        },
    )


def test_mantle_catalog_filters_to_available_openai_models():
    ep._MANTLE_CACHE.clear()
    with _patch_catalog(_mantle_payload()):
        models = _real_list_mantle_models(region="us-east-2")

    ids = [m["id"] for m in models]
    assert "openai/bedrock/gpt-5.6-sol" in ids
    assert "openai/bedrock/gpt-5.5" in ids
    # status != available is excluded
    assert "openai/bedrock/gpt-5.9-unreleased" not in ids
    # non-OpenAI vendors on the same endpoint are excluded
    assert not any("claude" in i or "qwen" in i for i in ids)


def test_mantle_catalog_labels_dated_snapshots():
    """Pinned snapshots get a readable label, not a raw ID."""
    ep._MANTLE_CACHE.clear()
    with _patch_catalog(_mantle_payload()):
        models = _real_list_mantle_models(region="us-east-2")

    by_id = {m["id"]: m["name"] for m in models}
    assert by_id["openai/bedrock/gpt-5.5-2026-04-23"].startswith("GPT-5.5 (2026-04-23)")
    assert by_id["openai/bedrock/gpt-5.6-sol"].startswith("GPT-5.6 Sol")


def test_mantle_catalog_returns_none_when_unreachable():
    """A catalog failure must degrade to None so the static list can answer.

    Returning [] instead would make list_available_models report zero OpenAI
    models — indistinguishable from "your account has no access".
    """
    ep._MANTLE_CACHE.clear()

    def _boom(*a, **k):
        raise RuntimeError("no network")

    with patch.dict(
        "sys.modules",
        {"httpx": type("httpx", (), {"get": staticmethod(_boom)})},
    ):
        assert _real_list_mantle_models(region="us-east-2") is None


def test_mantle_empty_openai_section_treated_as_unknown():
    """Zero OpenAI models means 'response shape changed', not 'none exist'."""
    ep._MANTLE_CACHE.clear()
    with _patch_catalog({"data": [{"id": "qwen.qwen3-32b", "status": "available"}]}):
        assert _real_list_mantle_models(region="us-east-2") is None


def test_mantle_catalog_is_cached_per_region():
    """A second call for the same region must not re-fetch."""
    ep._MANTLE_CACHE.clear()
    calls = []

    class _Response:
        def raise_for_status(self):
            pass

        def json(self):
            calls.append(1)
            return _mantle_payload()

    with patch.dict(
        "sys.modules",
        {
            "httpx": type("httpx", (), {"get": staticmethod(lambda *a, **k: _Response())}),
            "aws_bedrock_token_generator": type(
                "tokgen", (), {"provide_token": staticmethod(lambda **k: "tok")}
            ),
        },
    ):
        first = _real_list_mantle_models(region="us-east-2")
        second = _real_list_mantle_models(region="us-east-2")

    assert first == second
    assert len(calls) == 1


def test_cache_holds_multiple_regions_simultaneously():
    """Probing several regions must not evict earlier ones.

    find_mantle_regions_for_model() walks 4 regions to report where a model
    lives; a single-slot cache would make every probe a fresh HTTP call and
    then leave the cache holding whichever region happened to be last.
    """
    ep._MANTLE_CACHE.clear()
    with _patch_catalog(_mantle_payload()):
        _real_list_mantle_models(region="us-east-1")
        _real_list_mantle_models(region="us-west-2")

    assert set(ep._MANTLE_CACHE) == {"us-east-1", "us-west-2"}


def test_find_regions_for_model_reports_where_it_lives():
    """The region probe that turns 'not available' into an actionable message."""
    per_region = {
        "us-east-1": [{"id": "openai/bedrock/gpt-5.6-sol", "name": "s"}],
        "us-east-2": [{"id": "openai/bedrock/gpt-5.6-sol", "name": "s"}],
        "us-west-2": [{"id": "openai/bedrock/gpt-5.6-luna", "name": "l"}],
        "eu-west-1": None,  # unreachable
    }
    with patch.object(ep, "list_mantle_models", lambda region=None: per_region.get(region)):
        assert ep.find_mantle_regions_for_model("openai/bedrock/gpt-5.6-sol") == [
            "us-east-1",
            "us-east-2",
        ]
        assert ep.find_mantle_regions_for_model("openai/bedrock/gpt-5.6-luna") == ["us-west-2"]
        assert ep.find_mantle_regions_for_model("openai/bedrock/gpt-nope") == []


def test_find_regions_accepts_bare_mantle_id():
    """Callers may pass either ID form; both must resolve."""
    with patch.object(
        ep,
        "list_mantle_models",
        lambda region=None: [{"id": "openai/bedrock/gpt-5.5", "name": "x"}]
        if region == "us-east-2"
        else None,
    ):
        assert ep.find_mantle_regions_for_model("openai.gpt-5.5") == ["us-east-2"]
        assert ep.find_mantle_regions_for_model("openai/bedrock/gpt-5.5") == ["us-east-2"]


def test_live_catalog_preferred_over_static_list():
    """When the catalog answers, its region-accurate list wins."""
    live = [{"id": "openai/bedrock/gpt-5.6-sol", "name": "GPT-5.6 Sol"}]
    with patch.object(ep, "_provider_enabled", _enabled), patch.object(
        ep, "list_mantle_models", lambda *a, **k: live
    ):
        ids = [m["id"] for m in ep.get_external_models("bedrock-mantle")]
    # Only the live entry — the static gpt-5.4 must not leak through.
    assert ids == ["openai/bedrock/gpt-5.6-sol"]


# ---------------------------------------------------------------------------
# resolve_mantle_region — making GPT-5.x work for users outside us-east
# ---------------------------------------------------------------------------


def test_no_routing_when_home_region_serves_the_model():
    """The common case must cost nothing: no hop, no probing."""
    home = [{"id": "openai/bedrock/gpt-5.6-luna", "name": "l"}]
    with patch.object(ep, "list_mantle_models", lambda region=None: home), patch.object(
        ep, "find_mantle_regions_for_model", lambda _m: ["us-east-1"]
    ):
        assert ep.resolve_mantle_region("openai/bedrock/gpt-5.6-luna") is None


def test_routes_to_another_region_when_home_lacks_the_model():
    def _list(region=None):
        return (
            [{"id": "openai/bedrock/gpt-5.6-luna", "name": "l"}]
            if region == "us-west-2"
            else None
        )

    with patch.object(ep, "list_mantle_models", _list), patch.object(
        ep, "find_mantle_regions_for_model", lambda _m: ["us-east-1", "us-east-2"]
    ), patch(
        "eval_mcp.core.bedrock_client.resolve_region", lambda *a, **k: "us-west-2"
    ):
        assert ep.resolve_mantle_region("openai/bedrock/gpt-5.6-sol") == "us-east-1"


def test_never_routes_to_the_home_region_itself():
    """Returning the home region would be a pointless no-op override."""
    with patch.object(ep, "list_mantle_models", lambda region=None: None), patch.object(
        ep, "find_mantle_regions_for_model", lambda _m: ["us-west-2"]
    ), patch(
        "eval_mcp.core.bedrock_client.resolve_region", lambda *a, **k: "us-west-2"
    ):
        assert ep.resolve_mantle_region("openai/bedrock/gpt-5.5") is None


def test_pinned_region_env_var_wins(monkeypatch):
    monkeypatch.setenv("EVAL_MCP_MANTLE_REGION", "eu-west-1")
    assert ep.resolve_mantle_region("openai/bedrock/gpt-5.5") == "eu-west-1"


def test_cross_region_can_be_disabled(monkeypatch):
    """Data-residency escape hatch: prefer a clear failure over a silent hop."""
    monkeypatch.setenv("EVAL_MCP_NO_CROSS_REGION", "1")
    with patch.object(ep, "list_mantle_models", lambda region=None: None), patch.object(
        ep, "find_mantle_regions_for_model", lambda _m: ["us-east-1"]
    ):
        assert ep.resolve_mantle_region("openai/bedrock/gpt-5.6-sol") is None
