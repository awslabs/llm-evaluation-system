"""Tests for external/Mantle provider discovery + filtering."""

from unittest.mock import patch

from eval_mcp.tools import external_providers as ep


def _enabled(config):
    """Treat every provider as enabled regardless of keys/AWS creds."""
    return True


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
