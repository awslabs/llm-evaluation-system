"""Tests for the redacted-reasoning patch on the Converse path.

GPT-5.x returns its chain of thought as ``reasoningContent.redactedContent`` —
an encrypted blob — where Claude returns readable ``reasoningText``. Upstream's
schema requires ``reasoningText``, so the raw response cannot even be parsed.

The patch makes ``reasoningText`` optional and DROPS redacted-only blocks. The
drop is the load-bearing part: an earlier version substituted an empty
``reasoningText`` instead, which parsed fine but left a contentless reasoning
block in the message history. On the next turn Inspect serialized it back as
``reasoningText.text = ""`` and Converse rejected the request with a bare
``InternalServerException``. Verified live on gpt-5.6-luna (aiwf, 2026-08-20):
3/3 runs died at turn 3, and because ``fail_on_error`` was disabled they all
reported ``status: success`` with ``results: null``.

So these tests pin both directions — parse in, and nothing empty back out.
"""

import asyncio

import eval_mcp.inspect_patches  # noqa: F401 — applies on import
from inspect_ai.model._providers.bedrock import (
    ConverseMessage,
    ConverseMessageContent,
    ConverseMetrics,
    ConverseOutput,
    ConverseResponse,
    ConverseUsage,
    converse_contents,
    model_output_from_response,
)

REDACTED_BLOB = b"\x01\x02encrypted-reasoning-blob"


def _response(*content: dict) -> ConverseResponse:
    """A minimal assistant Converse response carrying `content` blocks."""
    return ConverseResponse(
        output=ConverseOutput(
            message=ConverseMessage(
                role="assistant",
                content=[ConverseMessageContent(**c) for c in content],
            )
        ),
        stopReason="end_turn",
        usage=ConverseUsage(inputTokens=1, outputTokens=1, totalTokens=2),
        metrics=ConverseMetrics(latencyMs=1),
    )


# ----- inbound: the payload must parse at all -----


def test_redacted_only_payload_parses() -> None:
    """Unpatched this raises ValidationError: reasoningText is required and
    there is no redactedContent field."""
    block = ConverseMessageContent(
        reasoningContent={"redactedContent": REDACTED_BLOB}
    )
    assert block.reasoningContent is not None
    assert block.reasoningContent.reasoningText is None


def test_readable_reasoning_still_parses() -> None:
    """Making reasoningText optional must not stop normal reasoning parsing."""
    block = ConverseMessageContent(
        reasoningContent={"reasoningText": {"text": "step one"}}
    )
    assert block.reasoningContent.reasoningText.text == "step one"


# ----- inbound: redacted blocks are dropped, not emptied -----


def test_redacted_block_is_dropped_from_output() -> None:
    response = _response(
        {"reasoningContent": {"redactedContent": REDACTED_BLOB}},
        {"text": "Workshop day is Tuesday."},
    )
    out = model_output_from_response("bedrock/us.openai.gpt-5.6-luna", response, [])
    content = out.choices[0].message.content
    types = [c.type for c in content] if isinstance(content, list) else ["text"]
    assert "reasoning" not in types, (
        "a redacted block must be dropped, not turned into empty reasoning — "
        "an empty reasoning block poisons the next turn's history"
    )
    assert "text" in types


def test_readable_reasoning_is_preserved_in_output() -> None:
    """Only redacted-only blocks are dropped; real reasoning survives."""
    response = _response(
        {"reasoningContent": {"reasoningText": {"text": "thinking hard"}}},
        {"text": "Answer."},
    )
    out = model_output_from_response("bedrock/us.anthropic.claude-sonnet-5", response, [])
    content = out.choices[0].message.content
    reasoning = [c for c in content if c.type == "reasoning"]
    assert len(reasoning) == 1
    assert reasoning[0].reasoning == "thinking hard"


# ----- outbound: the actual regression -----


def test_no_empty_reasoning_block_is_ever_sent_back() -> None:
    """The round trip that used to crash: parse a redacted response, then
    re-serialize that message for the following turn. The outbound payload
    must contain no reasoningContent at all — Converse answers an empty
    reasoningText with InternalServerException."""
    response = _response(
        {"reasoningContent": {"redactedContent": REDACTED_BLOB}},
        {"text": "Workshop day is Tuesday."},
    )
    out = model_output_from_response("bedrock/us.openai.gpt-5.6-luna", response, [])

    replayed = asyncio.run(converse_contents(out.choices[0].message.content))

    assert all(b.reasoningContent is None for b in replayed), (
        "replaying a reasoning block built from redacted content is what "
        "produced the live InternalServerException at turn 3"
    )
    assert any(b.text for b in replayed), "the spoken text must still be replayed"


def test_readable_reasoning_round_trips_outbound() -> None:
    """Guard against over-dropping: Claude's readable reasoning must still be
    sent back, since that path was never broken."""
    response = _response(
        {"reasoningContent": {"reasoningText": {"text": "thinking hard"}}},
        {"text": "Answer."},
    )
    out = model_output_from_response("bedrock/us.anthropic.claude-sonnet-5", response, [])

    replayed = asyncio.run(converse_contents(out.choices[0].message.content))

    emitted = [b.reasoningContent for b in replayed if b.reasoningContent is not None]
    assert len(emitted) == 1
    assert emitted[0].reasoningText.text == "thinking hard"
