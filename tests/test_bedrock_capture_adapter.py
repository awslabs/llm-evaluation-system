"""Adapter-level tests for bedrock_capture.

Botocore's Bedrock instrumentation emits one OTel log record per message in
each converse call (see
`opentelemetry/instrumentation/botocore/extensions/bedrock_utils.py`):

  - gen_ai.system.message     → body={"content": <system prompt>}
  - gen_ai.user.message       → body={"content": <user content>}
  - gen_ai.assistant.message  → body={"content": <...>, "tool_calls": [...]}
  - gen_ai.tool.message       → body={"id": ..., "content": [...]}  (tool result)
  - gen_ai.choice             → body={"message": {...}, "finish_reason": ...}

The adapter's job is to fold those records back into one Inspect ModelEvent
per converse call — preserving every role, every tool call, and every tool
result. If the adapter drops any of these, scoring sees a garbled trajectory
and the whole eval is unreliable.

These tests feed realistic sequences into the adapter and assert the emitted
ModelEvent contains the full conversation, not a last-write-wins subset.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import pytest

from eval_mcp.bedrock_capture import _InspectLogExporter


# ---------------------------------------------------------------------------
# Duck-typed fakes — the adapter reads `.log_record.body` and `.log_record.event_name`
# ---------------------------------------------------------------------------


@dataclass
class _FakeLogRecord:
    body: Any
    event_name: Optional[str] = None


@dataclass
class _FakeLogData:
    log_record: _FakeLogRecord


def _rec(event_name: str, body: Any) -> _FakeLogData:
    return _FakeLogData(log_record=_FakeLogRecord(body=body, event_name=event_name))


# ---------------------------------------------------------------------------
# Transcript capture — lets us inspect what the adapter emitted without
# depending on Inspect's contextvar-based transcript()
# ---------------------------------------------------------------------------


class _CapturingTranscript:
    def __init__(self):
        self.events = []

    def _event(self, event):
        self.events.append(event)


@pytest.fixture
def capture_transcript(monkeypatch):
    """Redirect transcript()._event(...) calls into a list we can assert on."""
    fake = _CapturingTranscript()
    import eval_mcp.bedrock_capture as bc

    monkeypatch.setattr(bc, "transcript", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_single_turn_preserves_user_prompt(capture_transcript):
    """Simplest case: one user message → one assistant reply.

    Baseline. If this fails we have bigger problems.
    """
    exporter = _InspectLogExporter()
    exporter.export([
        _rec("gen_ai.user.message", {"content": [{"text": "hello"}]}),
        _rec("gen_ai.choice", {
            "message": {"role": "assistant", "content": [{"text": "hi there"}]},
            "finish_reason": "end_turn",
        }),
    ])

    assert len(capture_transcript.events) == 1
    ev = capture_transcript.events[0]

    # Input should contain the user prompt.
    assert len(ev.input) == 1
    assert ev.input[0].role == "user"
    assert "hello" in str(ev.input[0].content)

    # Output should contain the assistant reply.
    assert ev.output.choices[0].message.content == "hi there"


def test_multi_message_conversation_preserves_all_roles(capture_transcript):
    """Realistic converse call: system + user + (prior) assistant + tool result + choice.

    This is what gets sent on a tool-loop continuation. The current adapter
    overwrites `_pending_input` on every record, so only the LAST input
    record survives. This test asserts every role is represented.
    """
    exporter = _InspectLogExporter()
    exporter.export([
        _rec("gen_ai.system.message", {"content": [{"text": "You are helpful."}]}),
        _rec("gen_ai.user.message", {"content": [{"text": "What's the weather in Seattle?"}]}),
        _rec("gen_ai.assistant.message", {
            "content": [{"text": ""}],
            "tool_calls": [{
                "id": "tooluse_1",
                "function": {"name": "get_weather", "arguments": {"city": "Seattle"}},
            }],
        }),
        _rec("gen_ai.tool.message", {
            "id": "tooluse_1",
            "content": [{"text": "14C, overcast with light rain"}],
        }),
        _rec("gen_ai.choice", {
            "message": {
                "role": "assistant",
                "content": [{"text": "It's 14°C and overcast in Seattle."}],
            },
            "finish_reason": "end_turn",
        }),
    ])

    assert len(capture_transcript.events) == 1
    ev = capture_transcript.events[0]

    # All four input messages should be present.
    roles = [getattr(m, "role", None) for m in ev.input]
    assert roles.count("system") >= 1, f"missing system message; got roles={roles}"
    assert roles.count("user") >= 1, f"missing user message; got roles={roles}"
    assert roles.count("assistant") >= 1, f"missing assistant message; got roles={roles}"
    assert roles.count("tool") >= 1, f"missing tool message; got roles={roles}"

    # Content of each surviving message should be non-empty.
    all_content = " ".join(str(m.content) for m in ev.input)
    assert "You are helpful" in all_content, "system prompt was dropped"
    assert "Seattle" in all_content, "user prompt was dropped"
    assert "14C" in all_content or "overcast" in all_content, "tool result was dropped"


def test_assistant_tool_call_is_preserved(capture_transcript):
    """A prior-turn assistant message with a tool_call must round-trip.

    Scorers look at the tool-call trajectory — if we drop the tool_calls
    array we can't score 'did the agent pick the right tool'.
    """
    exporter = _InspectLogExporter()
    exporter.export([
        _rec("gen_ai.user.message", {"content": [{"text": "compute 2+2"}]}),
        _rec("gen_ai.assistant.message", {
            "content": [{"text": ""}],
            "tool_calls": [{
                "id": "tooluse_add",
                "function": {"name": "add", "arguments": {"a": 2, "b": 2}},
            }],
        }),
        _rec("gen_ai.tool.message", {
            "id": "tooluse_add",
            "content": [{"text": "4"}],
        }),
        _rec("gen_ai.choice", {
            "message": {"role": "assistant", "content": [{"text": "2+2=4"}]},
            "finish_reason": "end_turn",
        }),
    ])

    ev = capture_transcript.events[0]

    assistant_msgs = [m for m in ev.input if getattr(m, "role", None) == "assistant"]
    assert len(assistant_msgs) >= 1, "prior assistant turn dropped"

    # The prior assistant turn must carry its tool_calls so the scorer can
    # see what the agent actually did on the earlier step.
    prior = assistant_msgs[0]
    tool_calls = getattr(prior, "tool_calls", None) or []
    assert len(tool_calls) == 1, f"prior assistant tool_calls dropped; got {tool_calls!r}"
    assert tool_calls[0].function == "add"
    assert tool_calls[0].arguments == {"a": 2, "b": 2}


def test_tool_result_links_to_tool_call_id(capture_transcript):
    """Tool-result messages carry a tool_call_id so Inspect can chain them
    back to the assistant call that requested them."""
    exporter = _InspectLogExporter()
    exporter.export([
        _rec("gen_ai.user.message", {"content": [{"text": "x"}]}),
        _rec("gen_ai.tool.message", {
            "id": "tooluse_42",
            "content": [{"text": "the-result"}],
        }),
        _rec("gen_ai.choice", {
            "message": {"role": "assistant", "content": [{"text": "done"}]},
            "finish_reason": "end_turn",
        }),
    ])

    ev = capture_transcript.events[0]
    tool_msgs = [m for m in ev.input if getattr(m, "role", None) == "tool"]
    assert len(tool_msgs) == 1
    # Inspect uses `tool_call_id` on ChatMessageTool to correlate.
    assert getattr(tool_msgs[0], "tool_call_id", None) == "tooluse_42"
    assert "the-result" in str(tool_msgs[0].content)


def test_multiple_tool_calls_in_one_assistant_turn(capture_transcript):
    """A single assistant turn can emit multiple tool_calls (parallel tool use).

    Botocore emits a single gen_ai.assistant.message with a tool_calls list —
    we must preserve every tool call, not just the first.
    """
    exporter = _InspectLogExporter()
    exporter.export([
        _rec("gen_ai.user.message", {"content": [{"text": "weather in SEA and PHX"}]}),
        _rec("gen_ai.assistant.message", {
            "content": [{"text": ""}],
            "tool_calls": [
                {"id": "t1", "function": {"name": "get_weather", "arguments": {"city": "Seattle"}}},
                {"id": "t2", "function": {"name": "get_weather", "arguments": {"city": "Phoenix"}}},
            ],
        }),
        _rec("gen_ai.tool.message", {"id": "t1", "content": [{"text": "14C"}]}),
        _rec("gen_ai.tool.message", {"id": "t2", "content": [{"text": "38C"}]}),
        _rec("gen_ai.choice", {
            "message": {"role": "assistant", "content": [{"text": "SEA 14C, PHX 38C"}]},
            "finish_reason": "end_turn",
        }),
    ])

    ev = capture_transcript.events[0]
    assistant_msgs = [m for m in ev.input if getattr(m, "role", None) == "assistant"]
    assert len(assistant_msgs) == 1
    tool_calls = assistant_msgs[0].tool_calls or []
    assert len(tool_calls) == 2
    assert {tc.function for tc in tool_calls} == {"get_weather"}
    assert {tc.id for tc in tool_calls} == {"t1", "t2"}

    tool_msgs = [m for m in ev.input if getattr(m, "role", None) == "tool"]
    assert {m.tool_call_id for m in tool_msgs} == {"t1", "t2"}


def test_assistant_choice_tool_calls_are_preserved(capture_transcript):
    """The final choice can itself be a tool_use response (not end_turn).

    If the scorer looks at `ev.output.choices[0].message.tool_calls` to judge
    'did the agent pick the right tool', we must pass them through.
    """
    exporter = _InspectLogExporter()
    exporter.export([
        _rec("gen_ai.user.message", {"content": [{"text": "compute 2+2"}]}),
        _rec("gen_ai.choice", {
            "message": {
                "role": "assistant",
                "content": [{"text": ""}],
                "tool_calls": [{
                    "id": "tooluse_add",
                    "function": {"name": "add", "arguments": {"a": 2, "b": 2}},
                }],
            },
            "finish_reason": "tool_use",
        }),
    ])

    ev = capture_transcript.events[0]
    out_msg = ev.output.choices[0].message
    tool_calls = out_msg.tool_calls or []
    assert len(tool_calls) == 1
    assert tool_calls[0].function == "add"
    assert tool_calls[0].arguments == {"a": 2, "b": 2}
    # Finish reason must map to 'tool_calls' so scorers that branch on this
    # field see the agent attempted a tool.
    assert ev.output.choices[0].stop_reason == "tool_calls"


def test_unicode_content_round_trips(capture_transcript):
    """Non-ASCII text (accents, emoji, CJK) must survive the adapter."""
    exporter = _InspectLogExporter()
    exporter.export([
        _rec("gen_ai.user.message", {"content": [{"text": "Qué tiempo hace en São Paulo? 🌤️"}]}),
        _rec("gen_ai.choice", {
            "message": {"role": "assistant", "content": [{"text": "晴れ、28°C"}]},
            "finish_reason": "end_turn",
        }),
    ])

    ev = capture_transcript.events[0]
    assert "São Paulo" in str(ev.input[0].content)
    assert "🌤️" in str(ev.input[0].content)
    assert "晴れ" in ev.output.choices[0].message.content


def test_unknown_event_names_are_ignored_not_crashed(capture_transcript):
    """Forward-compat: if OTel adds a new event_name we don't know about,
    we should skip it rather than blow up or misinterpret it as a known role.
    """
    exporter = _InspectLogExporter()
    exporter.export([
        _rec("gen_ai.user.message", {"content": [{"text": "hi"}]}),
        _rec("gen_ai.mystery.message", {"content": [{"text": "from the future"}]}),
        _rec("gen_ai.choice", {
            "message": {"role": "assistant", "content": [{"text": "hello"}]},
            "finish_reason": "end_turn",
        }),
    ])

    # Must not crash; unknown record contributes nothing to input.
    ev = capture_transcript.events[0]
    roles = [getattr(m, "role", None) for m in ev.input]
    assert "user" in roles
    # The mystery record has no recognized role so it should not appear.
    assert "mystery" not in roles
    # And its content must not have leaked into any real message.
    assert "from the future" not in " ".join(str(m.content) for m in ev.input)


def test_empty_body_record_does_not_crash(capture_transcript):
    """Some records may arrive with body=None (e.g. when capture_content=false
    upstream). The adapter must not crash."""
    exporter = _InspectLogExporter()
    exporter.export([
        _rec("gen_ai.user.message", None),
        _rec("gen_ai.choice", {
            "message": {"role": "assistant", "content": [{"text": "ok"}]},
            "finish_reason": "end_turn",
        }),
    ])

    # Exactly one event emitted, no exception.
    assert len(capture_transcript.events) == 1
    # User message still accounted for in input order even though empty.
    assert any(getattr(m, "role", None) == "user" for m in capture_transcript.events[0].input)


def test_multiple_converse_calls_do_not_leak_state(capture_transcript):
    """Two successive converse calls must produce two independent events.

    Accumulated input records from call #1 must NOT bleed into call #2's
    input, or we'll misattribute conversation history.
    """
    exporter = _InspectLogExporter()

    # Call 1
    exporter.export([
        _rec("gen_ai.user.message", {"content": [{"text": "first question"}]}),
        _rec("gen_ai.choice", {
            "message": {"role": "assistant", "content": [{"text": "first answer"}]},
            "finish_reason": "end_turn",
        }),
    ])
    # Call 2
    exporter.export([
        _rec("gen_ai.user.message", {"content": [{"text": "second question"}]}),
        _rec("gen_ai.choice", {
            "message": {"role": "assistant", "content": [{"text": "second answer"}]},
            "finish_reason": "end_turn",
        }),
    ])

    assert len(capture_transcript.events) == 2

    ev1, ev2 = capture_transcript.events
    assert "first question" in " ".join(str(m.content) for m in ev1.input)
    # Critical: ev2's input must NOT contain first-call content.
    ev2_content = " ".join(str(m.content) for m in ev2.input)
    assert "first question" not in ev2_content, "state leaked between calls"
    assert "second question" in ev2_content
