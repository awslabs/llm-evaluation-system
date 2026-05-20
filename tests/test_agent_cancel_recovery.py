"""Locks in the post-cancel conversation-history fix.

When a request is cancelled mid-tool-execution, the previous assistant
turn ends with a ``tool_use`` block that has no matching ``tool_result``.
Bedrock's Converse API rejects this. The agent stitches it up via
``_build_user_message`` by bundling synthetic ``tool_result`` blocks
into the NEXT user message — bundled, NOT appended as a separate user
message, since two consecutive user messages also error out on Bedrock.

These tests assert that bundling, since regressing it surfaces as a
generic "Sorry, I encountered an error" on the next chat turn.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.core.agent import Agent


def _new_agent() -> Agent:
    # Bypass __init__'s side-effects (system prompt loading, etc.) — we
    # only need conversation_history + cancel_info.
    agent = Agent.__new__(Agent)
    agent.conversation_history = []
    agent.cancel_info = {}
    return agent


def test_no_orphans_returns_user_string_unchanged():
    """When the prior assistant turn has no tool_use, the new user
    message is a plain string — same as before the fix."""
    agent = _new_agent()
    agent.conversation_history.append({"role": "user", "content": "hi"})
    agent.conversation_history.append({"role": "assistant", "content": "hello!"})

    msg = agent._build_user_message("how are you")
    assert msg == {"role": "user", "content": "how are you"}


def test_orphan_tool_use_bundles_into_next_user_message():
    """The fix in action: a hanging tool_use from a cancelled turn must
    get its tool_result bundled into the SAME user message as the new
    text — not into a separate user message before it."""
    agent = _new_agent()
    agent.conversation_history = [
        {"role": "user", "content": "list models"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "toolu_1", "name": "list_bedrock_models", "input": {}},
        ]},
    ]

    msg = agent._build_user_message("sonnet 4.6 vs sonnet 4")

    assert msg["role"] == "user"
    assert isinstance(msg["content"], list), (
        "must be a content-block list so tool_result + text live in one message"
    )
    # First block(s) are tool_result(s) closing out the orphan tool_use
    assert msg["content"][0]["type"] == "tool_result"
    assert msg["content"][0]["tool_use_id"] == "toolu_1"
    # Last block is the new user text
    assert msg["content"][-1]["type"] == "text"
    assert msg["content"][-1]["text"] == "sonnet 4.6 vs sonnet 4"


def test_orphan_with_eval_cancel_info_uses_resume_hint():
    """When cancel_info carries an evalId, the cancel message includes
    the resume hint so the agent can offer the user a way to recover."""
    agent = _new_agent()
    agent.conversation_history = [
        {"role": "user", "content": "run eval"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_2", "name": "run_evaluation", "input": {}},
        ]},
    ]
    agent.cancel_info = {"evalId": "eval_abc123", "configName": "my-config"}

    msg = agent._build_user_message("never mind, just list models")

    tr = msg["content"][0]
    assert tr["type"] == "tool_result"
    assert "eval_abc123" in tr["content"]
    assert "resume" in tr["content"].lower()
    # cancel_info is single-use — cleared after the message is built so
    # a subsequent recovery doesn't re-stuff the resume hint.
    assert agent.cancel_info == {}


def test_multiple_orphan_tool_uses_all_get_results():
    """A single assistant turn can call multiple tools in parallel; if
    cancelled, every tool_use needs a matching tool_result."""
    agent = _new_agent()
    agent.conversation_history = [
        {"role": "user", "content": "compare them"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_a", "name": "list_models", "input": {}},
            {"type": "tool_use", "id": "toolu_b", "name": "list_datasets", "input": {}},
        ]},
    ]

    msg = agent._build_user_message("ok continue")

    tool_results = [b for b in msg["content"] if b.get("type") == "tool_result"]
    assert len(tool_results) == 2
    assert {tr["tool_use_id"] for tr in tool_results} == {"toolu_a", "toolu_b"}


def test_cancel_mid_bedrock_stream_inserts_synthetic_assistant():
    """The other cancel-recovery case: the previous request was cancelled
    BEFORE any assistant turn was appended (cancel landed inside the
    Bedrock streaming await, before `_agentic_loop_streaming` reached
    its `conversation_history.append` step). History ends with a user
    message; naively appending another user message produces two
    consecutive user messages and Bedrock 400s.

    Build a user message in that state, and the call site must end up
    with a synthetic assistant turn between the two user messages so
    alternation is preserved.
    """
    agent = _new_agent()
    # Prior turn started but was cancelled mid-Bedrock-stream — only
    # the user message landed.
    agent.conversation_history = [
        {"role": "user", "content": "run a simple eval"},
    ]

    msg = agent._build_user_message("run a simple eval for me")

    # The build call should have injected a synthetic assistant turn
    # into history, and returned a fresh user message.
    assert msg == {"role": "user", "content": "run a simple eval for me"}
    assert len(agent.conversation_history) == 2
    assert agent.conversation_history[-1]["role"] == "assistant"
    assert "cancelled" in agent.conversation_history[-1]["content"].lower()


def test_history_after_build_alternates_roles():
    """Integration check: after _build_user_message is appended, the
    history walks user/assistant/user/assistant — no two consecutive
    user messages. This is the property Bedrock requires."""
    agent = _new_agent()
    agent.conversation_history = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_x", "name": "t", "input": {}},
        ]},
    ]
    agent.conversation_history.append(agent._build_user_message("next"))

    roles = [m["role"] for m in agent.conversation_history]
    # No two consecutive same-role entries
    for prev, curr in zip(roles, roles[1:]):
        assert prev != curr, f"consecutive same-role messages: {roles}"


def test_history_after_mid_stream_cancel_alternates():
    """Same alternation invariant for the mid-Bedrock-stream cancel
    shape: history starts with [user] only, and after the new user
    message is appended the result must still alternate."""
    agent = _new_agent()
    agent.conversation_history = [{"role": "user", "content": "first"}]
    agent.conversation_history.append(agent._build_user_message("second"))

    roles = [m["role"] for m in agent.conversation_history]
    assert roles == ["user", "assistant", "user"], roles
