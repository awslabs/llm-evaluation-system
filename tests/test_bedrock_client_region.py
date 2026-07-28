"""Tests for region resolution and reasoning-model temperature handling.

Both behaviors exist because model availability on Bedrock is region-specific
and model *parameter* support is model-specific:

  - OpenAI's frontier models launched region-by-region: as of 2026-07-27
    gpt-5.5 and gpt-5.6-sol are us-east-1/us-east-2 only, while gpt-5.6-terra,
    gpt-5.6-luna and gpt-5.4 are also in us-west-2. Every call site used to read
    `os.environ.get("AWS_REGION", "us-west-2")` directly, which ignored the
    user's configured profile region and made the us-east-only models
    unreachable — the tool would report they didn't exist.

  - Reasoning models reject `temperature` outright with HTTP 400
    `unsupported_parameter`, not a warning. Sending it is a hard failure.
"""

from __future__ import annotations

import pytest

from eval_mcp.core import bedrock_client as bc


@pytest.fixture(autouse=True)
def _clean_region_env(monkeypatch):
    """Start each test with no region env vars and autodetect already done.

    Marking autodetect done keeps resolve_region() from touching ~/.aws/config
    on the machine running the tests.
    """
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.setattr(bc, "_autodetect_done", True)
    monkeypatch.setattr(bc, "_autodetect_error", None)


def _no_profile_region(monkeypatch):
    """Make boto3's session report no configured region."""

    class _Session:
        region_name = None

    monkeypatch.setattr(bc.boto3, "Session", lambda *a, **k: _Session())


# ---------------------------------------------------------------------------
# resolve_region precedence
# ---------------------------------------------------------------------------


def test_explicit_argument_wins(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    assert bc.resolve_region("eu-central-1") == "eu-central-1"


def test_aws_region_beats_aws_default_region(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    assert bc.resolve_region() == "us-east-2"


def test_aws_default_region_used_when_aws_region_absent(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    assert bc.resolve_region() == "us-east-1"


def test_profile_region_used_when_env_is_empty(monkeypatch):
    """The fix: honour the resolved profile's region.

    A user whose profile says `region = us-east-2` previously still got
    us-west-2, putting gpt-5.5 and gpt-5.6-sol permanently out of reach with no
    recourse short of exporting AWS_REGION by hand.
    """

    class _Session:
        region_name = "us-east-2"

    monkeypatch.setattr(bc.boto3, "Session", lambda *a, **k: _Session())
    assert bc.resolve_region() == "us-east-2"


def test_falls_back_to_default_when_nothing_configured(monkeypatch):
    _no_profile_region(monkeypatch)
    assert bc.resolve_region() == bc.DEFAULT_REGION


def test_never_raises_when_session_construction_fails(monkeypatch):
    """A broken AWS config must not take down region resolution.

    resolve_region() is called on import-adjacent paths; raising here would
    surface as "MCP failed to connect" rather than a credential error.
    """

    def _boom(*a, **k):
        raise RuntimeError("no config for you")

    monkeypatch.setattr(bc.boto3, "Session", _boom)
    assert bc.resolve_region() == bc.DEFAULT_REGION


def test_empty_string_env_does_not_shadow_profile(monkeypatch):
    """AWS_REGION="" is unset, not a region.

    Docker/Helm templating routinely produces empty env values; treating "" as
    a region would send requests to a nonsense endpoint.
    """
    monkeypatch.setenv("AWS_REGION", "")

    class _Session:
        region_name = "us-east-1"

    monkeypatch.setattr(bc.boto3, "Session", lambda *a, **k: _Session())
    assert bc.resolve_region() == "us-east-1"


# ---------------------------------------------------------------------------
# Reasoning models and the temperature parameter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id",
    [
        "openai.gpt-5.6-sol",
        "openai.gpt-5.6-luna",
        "openai.gpt-5.6-terra",
        "openai.gpt-5.5",
        "openai/bedrock/gpt-5.4",
        "openai.gpt-5.7-hypothetical-future",  # forward-compatible by design
        "o3-mini",
        "o4-mini",
    ],
)
def test_reasoning_models_reject_temperature(model_id):
    assert bc.model_rejects_temperature(model_id) is True


@pytest.mark.parametrize(
    "model_id",
    [
        "us.anthropic.claude-sonnet-4-6",
        "anthropic.claude-haiku-4-5",
        "openai.gpt-oss-120b",  # open-weight, accepts temperature
        "meta.llama3-3-70b-instruct-v1:0",
    ],
)
def test_standard_models_accept_temperature(model_id):
    assert bc.model_rejects_temperature(model_id) is False


class _StubClient(bc.BedrockClient):
    """BedrockClient with the boto3 client and singleton machinery bypassed."""

    def __init__(self, model_id):  # noqa: D107 - test double
        self.model_id = model_id
        self.region = "us-east-2"
        self._initialized = True

    def __new__(cls, *a, **k):  # bypass the per-region singleton cache
        return object.__new__(cls)


def test_request_body_omits_temperature_for_reasoning_model():
    """The 400 this prevents: 'temperature' is not supported with this model."""
    body = _StubClient("openai.gpt-5.6-luna")._build_request_body(
        [{"role": "user", "content": "hi"}], max_tokens=64, temperature=0.0
    )
    assert "temperature" not in body


def test_request_body_keeps_temperature_for_claude():
    """Claude callers still get the determinism they asked for."""
    body = _StubClient("us.anthropic.claude-sonnet-4-6")._build_request_body(
        [{"role": "user", "content": "hi"}], max_tokens=64, temperature=0.0
    )
    assert body["temperature"] == 0.0


def test_explicit_none_temperature_is_omitted():
    """Callers can opt out explicitly regardless of model."""
    body = _StubClient("us.anthropic.claude-sonnet-4-6")._build_request_body(
        [{"role": "user", "content": "hi"}], max_tokens=64, temperature=None
    )
    assert "temperature" not in body


def test_request_body_always_has_required_anthropic_fields():
    body = _StubClient("us.anthropic.claude-sonnet-4-6")._build_request_body(
        [{"role": "user", "content": "hi"}], max_tokens=128, temperature=0.3
    )
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    assert body["max_tokens"] == 128
    assert body["messages"] == [{"role": "user", "content": "hi"}]


# ---------------------------------------------------------------------------
# Non-Anthropic response bodies must fail loudly
# ---------------------------------------------------------------------------


def test_openai_shaped_response_raises_with_actionable_message():
    """The silent-data-loss case: HTTP 200 but no `content` key.

    Bedrock happily serves gpt-oss via invoke_model, returning an OpenAI-shaped
    body with `choices`. Returning "" there meant callers treated an empty
    string as a real answer and failed later somewhere unrelated.
    """
    client = _StubClient("openai.gpt-oss-120b")
    openai_body = {"choices": [{"message": {"content": "hello"}}]}

    with pytest.raises(ValueError) as excinfo:
        client.extract_text_from_response(openai_body)

    message = str(excinfo.value)
    assert "BEDROCK_MODEL_ID" in message
    assert "openai.gpt-oss-120b" in message
    # Must not imply non-Anthropic models are unusable for evals — they aren't.
    assert "evaluation targets and judges" in message


def test_anthropic_response_still_extracts_text():
    client = _StubClient("us.anthropic.claude-sonnet-4-6")
    body = {
        "content": [
            {"type": "text", "text": "line one"},
            {"type": "tool_use", "name": "t", "input": {}},
            {"type": "text", "text": "line two"},
        ]
    }
    assert client.extract_text_from_response(body) == "line one\nline two"


def test_empty_content_list_is_not_an_error():
    """A model that legitimately returned only tool_use blocks yields ""."""
    client = _StubClient("us.anthropic.claude-sonnet-4-6")
    assert client.extract_text_from_response({"content": []}) == ""


def test_default_region_serves_the_us_east_only_models():
    """The fallback region must be one where the full model set exists.

    OpenAI's frontier models on Bedrock Mantle (gpt-5.5, gpt-5.6-sol) are served
    in us-east-1/us-east-2 ONLY. Defaulting anywhere else makes them unreachable
    and — as actually happened — has the tool report they do not exist. Every
    current-generation Converse model (Claude Opus 5 / Sonnet 5 / Haiku 4.5 /
    Sonnet 4.6, Nova, gpt-oss) is present in us-east-2 as well, so this costs
    nothing.

    Pinned as a test because the failure mode is silent: a well-meaning revert
    to us-west-2 breaks two models with no error until someone tries to use them.
    """
    assert bc.DEFAULT_REGION in ("us-east-1", "us-east-2"), (
        "DEFAULT_REGION must be a region that serves the Bedrock Mantle "
        "frontier models; see the comment on DEFAULT_REGION."
    )
