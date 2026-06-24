"""Tests for live/snapshot model pricing resolution.

Run fully offline (EVAL_MCP_PRICING_OFFLINE) so they exercise the vendored
snapshot deterministically without a network call.
"""

import importlib
import os

import pytest


@pytest.fixture
def pricing(monkeypatch, tmp_path):
    """Reload the pricing module in offline mode with an isolated cache dir."""
    monkeypatch.setenv("EVAL_MCP_PRICING_OFFLINE", "1")
    monkeypatch.setenv("EVAL_MCP_HOME", str(tmp_path))  # no stale cache
    import eval_mcp.core.pricing as p
    importlib.reload(p)
    return p


def test_bedrock_region_uplift(pricing):
    """The us. cross-region variant must price higher than the base model."""
    base = pricing.get_model_cost("bedrock/anthropic.claude-sonnet-4-5-20250929-v1:0")
    cross = pricing.get_model_cost("bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0")
    assert base is not None and cross is not None
    assert cross["input"] > base["input"], "cross-region uplift not applied"


def test_otel_aws_bedrock_prefix(pricing):
    """OTel semconv 'aws.bedrock/' resolves the same as Inspect's 'bedrock/'."""
    a = pricing.get_model_cost("bedrock/amazon.nova-pro-v1:0")
    b = pricing.get_model_cost("aws.bedrock/amazon.nova-pro-v1:0")
    assert a == b and a is not None


def test_cache_tiers_present(pricing):
    """Anthropic models carry cache read/write pricing from LiteLLM."""
    cost = pricing.get_model_cost("bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0")
    assert cost["input_cache_read"] > 0
    assert cost["input_cache_write"] > 0


def test_calculate_cost_math(pricing):
    """1M input + 0.5M output priced at the model's per-Mtok rates."""
    cost = pricing.get_model_cost("bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0")
    total = pricing.calculate_cost(
        "bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0", 1_000_000, 500_000
    )
    assert total == pytest.approx(cost["input"] + 0.5 * cost["output"])


def test_cache_tokens_add_cost(pricing):
    """Cache read tokens increase the computed cost."""
    mid = "bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    without = pricing.calculate_cost(mid, 1000, 1000)
    with_cache = pricing.calculate_cost(mid, 1000, 1000, cache_read_tokens=10_000)
    assert with_cache > without


def test_context_window_suffix_resolves(pricing):
    """AWS appends a ':128k' context qualifier that LiteLLM keys without —
    the matcher must strip it and still find the price."""
    cost = pricing.get_model_cost("bedrock/meta.llama3-3-70b-instruct-v1:0:128k")
    assert cost is not None and cost["input"] > 0


def test_unknown_model_returns_none(pricing):
    """Unknown models return None (distinct from a $0 cost)."""
    assert pricing.get_model_cost("bedrock/totally.invented-model-v9:0") is None
    assert pricing.calculate_cost("bedrock/totally.invented-model-v9:0", 100, 100) is None
