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


def test_bedrock_mantle_distinct_pricing(pricing):
    """Bedrock Mantle GPT-5.4 (openai/bedrock/...) must resolve the Mantle-specific
    price (bedrock_mantle/openai.gpt-5.4), NOT the cheaper OpenAI-direct gpt-5.4."""
    mantle = pricing.get_model_cost("openai/bedrock/gpt-5.4")
    direct = pricing.get_model_cost("gpt-5.4")
    assert mantle is not None and direct is not None
    # Mantle is priced higher than the OpenAI-direct API.
    assert mantle["input"] > direct["input"], (
        f"Mantle should use its own (higher) pricing, got mantle={mantle} direct={direct}"
    )


@pytest.mark.parametrize(
    "model_id",
    [
        "openai/bedrock/gpt-5.5",
        "openai/bedrock/gpt-5.6-sol",
        "openai/bedrock/gpt-5.6-terra",
        "openai/bedrock/gpt-5.6-luna",
    ],
)
def test_gpt5x_mantle_models_priced_offline(pricing, model_id):
    """Every GPT-5.x model we surface must price from the vendored snapshot.

    These IDs are advertised by list_available_models, so an unpriced one shows
    up in the viewer as a blank cost column. The snapshot lagged the gpt-5.6
    launch and returned None for all three variants until `make sync-pricing`.
    """
    cost = pricing.get_model_cost(model_id)
    assert cost is not None, f"{model_id} unpriced — run `make sync-pricing`"
    assert cost["input"] > 0 and cost["output"] > 0


def test_gpt56_price_tiers_ordered(pricing):
    """Sol > Terra > Luna, per AWS's positioning of the three variants.

    Guards against a snapshot refresh silently scrambling which ID maps to
    which price — the numbers are plausible individually but wrong in relation.
    """
    sol = pricing.get_model_cost("openai/bedrock/gpt-5.6-sol")
    terra = pricing.get_model_cost("openai/bedrock/gpt-5.6-terra")
    luna = pricing.get_model_cost("openai/bedrock/gpt-5.6-luna")
    assert sol["input"] > terra["input"] > luna["input"]


def test_dated_snapshot_falls_back_to_floating_alias(pricing):
    """Pinned date snapshots price as their floating alias.

    Mantle serves both `gpt-5.5` and `gpt-5.5-2026-04-23`; LiteLLM keys only the
    former. The pinned ID is the better choice for a reproducible eval, so
    reporting its cost as unknown would penalize the right decision.
    """
    dated = pricing.get_model_cost("openai/bedrock/gpt-5.5-2026-04-23")
    alias = pricing.get_model_cost("openai/bedrock/gpt-5.5")
    assert dated is not None
    assert dated == alias


def test_date_fallback_does_not_invent_prices(pricing):
    """The fallback must not price a model family that genuinely isn't listed."""
    assert pricing.get_model_cost("openai/bedrock/gpt-9.9-imaginary-2030-01-01") is None


def test_unknown_model_returns_none(pricing):
    """Unknown models return None (distinct from a $0 cost)."""
    assert pricing.get_model_cost("bedrock/totally.invented-model-v9:0") is None
    assert pricing.calculate_cost("bedrock/totally.invented-model-v9:0", 100, 100) is None
