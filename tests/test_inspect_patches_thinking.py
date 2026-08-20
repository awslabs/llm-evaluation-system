"""Tests for Claude-on-Bedrock native routing and the Converse limit patch.

Claude models route through Inspect's native anthropic provider
(``anthropic/bedrock/<id>``, the bedrock-runtime Messages API) so thinking
control comes from the upstream SDK provider instead of Inspect's Converse
mirror — see eval_mcp/core/model_routing.py. These tests pin the boundary
translation and the limit-discovery helper for the families still on Converse.
"""

from eval_mcp.core.model_routing import from_native, to_native
from eval_mcp.inspect_patches import _limit_from_result


# ----- model routing -----


def test_claude_bedrock_ids_route_native() -> None:
    assert (
        to_native("bedrock/global.anthropic.claude-sonnet-5")
        == "anthropic/bedrock/global.anthropic.claude-sonnet-5"
    )
    assert (
        to_native("bedrock/us.anthropic.claude-opus-5")
        == "anthropic/bedrock/us.anthropic.claude-opus-5"
    )
    assert (
        to_native("bedrock/us.anthropic.claude-3-haiku-20240307-v1:0")
        == "anthropic/bedrock/us.anthropic.claude-3-haiku-20240307-v1:0"
    )


def test_non_claude_ids_stay_on_converse() -> None:
    for mid in (
        "bedrock/us.amazon.nova-pro-v1:0",
        "bedrock/meta.llama3-70b-instruct-v1:0",
        "bedrock/openai.gpt-oss-120b-1:0",
    ):
        assert to_native(mid) == mid


def test_mantle_and_external_ids_untouched() -> None:
    for mid in (
        "openai/bedrock/gpt-5.4",
        "openai/gpt-5.4",
        "anthropic/claude-opus-5",
        "mockllm/model",
    ):
        assert to_native(mid) == mid


def test_to_native_is_idempotent() -> None:
    native = to_native("bedrock/global.anthropic.claude-sonnet-5")
    assert to_native(native) == native


def test_from_native_roundtrip() -> None:
    original = "bedrock/global.anthropic.claude-sonnet-5"
    assert from_native(to_native(original)) == original


def test_from_native_leaves_other_ids_alone() -> None:
    for mid in (
        "bedrock/us.amazon.nova-pro-v1:0",
        "openai/bedrock/gpt-5.4",
        "anthropic/claude-opus-5",  # first-party id, not a bedrock route
    ):
        assert from_native(mid) == mid


# ----- Converse max_tokens limit discovery -----


class _FakeValidationError(Exception):
    pass


def test_limit_parsed_from_validation_error() -> None:
    ex = _FakeValidationError(
        "An error occurred (ValidationException) when calling the Converse "
        "operation: The maximum tokens you requested exceeds the model limit "
        "of 10000. Try again with a maximum tokens value that is lower than 10000."
    )
    assert _limit_from_result((ex, object())) == 10000


def test_no_limit_for_success_or_unrelated_error() -> None:
    assert _limit_from_result("plain output") is None
    assert _limit_from_result((_FakeValidationError("too many input tokens"), object())) is None


# ----- thinking-control capability gate -----


def test_thinking_control_supported_routes() -> None:
    from eval_mcp.core.model_routing import supports_thinking_control

    for mid in (
        "bedrock/global.anthropic.claude-sonnet-5",
        "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "bedrock/openai.gpt-oss-120b-1:0",
        "bedrock/us.amazon.nova-pro-v1:0",
        "bedrock/global.openai.gpt-5.6-sol",  # via the inspect_patches branch
        "openai/bedrock/gpt-5.4",
        "openai/gpt-5.4",
        "anthropic/claude-opus-5",
    ):
        assert supports_thinking_control(mid), mid


def test_thinking_control_unsupported_routes() -> None:
    from eval_mcp.core.model_routing import supports_thinking_control

    for mid in (
        "bedrock/meta.llama3-70b-instruct-v1:0",
        "bedrock/mistral.mistral-large-3",
        "bedrock/deepseek.v3-2",
    ):
        assert not supports_thinking_control(mid), mid


def test_gpt5_reasoning_effort_emitted_on_converse() -> None:
    """The patched reasoning_config must emit the Responses-API shape for
    GPT-5.x — the models accept it via additionalModelRequestFields."""
    import eval_mcp.inspect_patches  # noqa: F401 — applies on import
    from inspect_ai.model import GenerateConfig
    from inspect_ai.model._providers.bedrock import BedrockAPI

    api = object.__new__(BedrockAPI)
    api.model_name = "global.openai.gpt-5.6-sol"
    fields = BedrockAPI.reasoning_config(api, GenerateConfig(reasoning_effort="low"))
    assert fields == {"reasoning": {"effort": "low"}}
    # no effort requested -> no injected fields
    assert BedrockAPI.reasoning_config(api, GenerateConfig()) == {}
    # gpt-oss keeps upstream's own mapping, not the gpt-5 shape
    api.model_name = "openai.gpt-oss-120b-1:0"
    fields = BedrockAPI.reasoning_config(api, GenerateConfig(reasoning_effort="low"))
    assert fields == {"reasoning_effort": "low"}
