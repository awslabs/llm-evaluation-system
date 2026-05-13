"""Tests for layered-span dedup in eval_results.

Background: Strands, LangChain, and other self-instrumented frameworks emit
their own GenAI spans alongside botocore's Bedrock spans. Both wrap the same
underlying HTTP call. Without dedup, the UI shows one model twice (once as
`strands-agents/...`, once as `aws.bedrock/...`) with mismatched token counts
because the framework typically under-counts tool-use loop iterations.

Rule: when both a provider system (aws.bedrock) and a framework system
(strands-agents, etc.) exist for the same model_id, the provider's tokens
are canonical and the framework entry is dropped. If only a framework span
exists (e.g. the framework targeted a non-Bedrock backend we don't instrument),
keep it so we don't silently lose data.
"""

from __future__ import annotations

from eval_mcp.core.eval_results import _dedupe_layered_model_usage, _split_model_key


def _u(input_tokens: int, output_tokens: int) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def test_split_model_key_normal_case():
    """The format ModelEvent.model uses: f'{system}/{model_id}'."""
    system, model_id = _split_model_key("aws.bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0")
    assert system == "aws.bedrock"
    assert model_id == "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_split_model_key_no_slash_returns_empty_system():
    """Defensive: if a span comes through without the system/ prefix, we
    treat it as 'system unknown' rather than crashing."""
    system, model_id = _split_model_key("just-a-model-name")
    assert system == ""
    assert model_id == "just-a-model-name"


def test_dedupe_drops_framework_entry_when_provider_present():
    """The exact bug from the UI: strands-agents and aws.bedrock both
    reported for the same model. Provider wins, framework is dropped.
    """
    raw = {
        "strands-agents/us.anthropic.claude-haiku-4-5": _u(789, 100),
        "aws.bedrock/us.anthropic.claude-haiku-4-5": _u(10000, 648),
    }
    deduped = _dedupe_layered_model_usage(raw)

    assert "strands-agents/us.anthropic.claude-haiku-4-5" not in deduped
    assert "aws.bedrock/us.anthropic.claude-haiku-4-5" in deduped
    # Provider tokens preserved unchanged.
    assert deduped["aws.bedrock/us.anthropic.claude-haiku-4-5"]["total_tokens"] == 10648


def test_dedupe_keeps_framework_when_no_provider_for_that_model():
    """Edge case: framework hit a backend we don't instrument (or instrumentor
    failed). All we have is framework data — must not silently drop it.
    """
    raw = {
        "strands-agents/us.anthropic.claude-haiku-4-5": _u(789, 100),
    }
    deduped = _dedupe_layered_model_usage(raw)

    # Framework entry survives because no provider entry exists for this model.
    assert "strands-agents/us.anthropic.claude-haiku-4-5" in deduped


def test_dedupe_handles_multiple_models_independently():
    """Two different models, each with both layers — dedupe each one."""
    raw = {
        "strands-agents/us.anthropic.claude-haiku-4-5": _u(700, 50),
        "aws.bedrock/us.anthropic.claude-haiku-4-5": _u(9000, 600),
        "strands-agents/us.anthropic.claude-sonnet-4-6": _u(1200, 80),
        "aws.bedrock/us.anthropic.claude-sonnet-4-6": _u(15000, 900),
    }
    deduped = _dedupe_layered_model_usage(raw)

    assert set(deduped.keys()) == {
        "aws.bedrock/us.anthropic.claude-haiku-4-5",
        "aws.bedrock/us.anthropic.claude-sonnet-4-6",
    }


def test_dedupe_passes_through_provider_only():
    """No framework spans at all (e.g. raw boto3 agent, no framework wrapper).
    Pass through unchanged."""
    raw = {
        "aws.bedrock/us.anthropic.claude-haiku-4-5": _u(500, 30),
    }
    deduped = _dedupe_layered_model_usage(raw)
    assert deduped == raw


def test_dedupe_empty_input():
    """No model events captured (e.g. agent crashed before any LLM call).
    Don't blow up; return an empty dict."""
    assert _dedupe_layered_model_usage({}) == {}


def test_dedupe_does_not_mutate_input():
    """Defensive: callers may keep a reference to the original dict and
    use it elsewhere. The dedupe must not modify in place."""
    raw = {
        "strands-agents/X": _u(100, 10),
        "aws.bedrock/X": _u(500, 30),
    }
    raw_copy = {k: dict(v) for k, v in raw.items()}
    _dedupe_layered_model_usage(raw)
    assert raw == raw_copy
