"""A Stop must not kill the NEXT message.

Live incident (us-east-2, 2026-09-22 19:47 UTC). The user clicked Stop on
a long eval at 19:46:17, then sent "can you use a tiny dataset this is
taking too long". That message came back as an instant "[Request
cancelled]" — 188 bytes in 78ms — and so did the resend. Both user rows
were persisted with no assistant reply, leaving the session ending in two
consecutive user turns, which Bedrock rejects on every later turn.

Cause: `cancel_chat` writes `cancelled_sessions[session_id]` on whichever
replica receives the Stop, while `run_agent_background`'s `finally` pops
it only on the replica that RAN the cancelled turn. With 2+ replicas and
no sticky routing those differ, so the receiving pod keeps the entry
forever, and the cancellation check reads that dict BEFORE any DB read.

Three guarantees pinned here:
  1. a new turn clears the in-memory flag first, on whatever pod handles it
  2. a cancel with no output still persists an assistant turn
  3. history hydration repairs transcripts already broken this way
"""
from __future__ import annotations

import contextlib
import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "_cancel_fakes", Path(__file__).with_name("test_chat_reconnect_empty_message.py")
)
_fakes = importlib.util.module_from_spec(_spec)
sys.modules["_cancel_fakes"] = _fakes
_spec.loader.exec_module(_fakes)


@pytest.fixture
def wired(monkeypatch):
    from backend.api import main
    db = _fakes._RecordingDB()
    bed = _fakes._ExplodingBedrock()
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "bedrock_client", bed)
    monkeypatch.setattr(main, "mcp_client", _fakes._FakeMCP())
    main.cancelled_sessions.clear()
    main.active_tasks.clear()
    main.event_queues.clear()
    return main, db, bed


# ---------------------------------------------------------------------------
# 1. The stale in-memory flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_cancel_flag_does_not_kill_the_next_message(wired):
    """The exact production failure: a leftover cancelled_sessions entry
    on the pod that received Stop must not cancel an unrelated new turn."""
    main, db, bed = wired
    session_id = "sess-stale-cancel"

    # State left behind on the Stop-receiving replica: its `finally`
    # never ran, because the task was on another pod.
    main.cancelled_sessions[session_id] = {"evalId": "eval_123"}

    chunks = await _fakes._drive_stream(main, session_id, "use a tiny dataset")

    names = _fakes._collect(chunks)
    assert "cancelled" not in names, (
        "A new message was cancelled by a previous Stop's leftover flag. "
        f"events={names}"
    )
    assert bed.called, "the new turn never reached the model"
    stored = await db.get_session_messages(session_id)
    assert [m["role"] for m in stored] == ["user", "assistant"], stored


@pytest.mark.asyncio
async def test_flag_is_cleared_before_the_turn_runs_not_after(wired):
    """Pin the mechanism, not just the symptom. The `finally` already pops
    the entry at the END of a turn, so asserting on the post-turn state
    passes even with the bug. What matters is that the flag is gone by the
    time the agent loop starts reading it."""
    main, _db, _bed = wired
    session_id = "sess-flag-cleared"
    main.cancelled_sessions[session_id] = {"evalId": "eval_456"}
    seen = {}

    class _ObservingAgent:
        async def run_conversation_turn_streaming(self, _msg):
            seen["flag_present_at_turn_start"] = session_id in main.cancelled_sessions
            yield {"type": "text", "data": {"content": "ok"}}
            yield {"type": "complete", "data": {"response": "ok"}}

    import asyncio
    await main.run_agent_background(
        session_id=session_id,
        user_id="u1",
        agent=_ObservingAgent(),
        final_message="hello",
        user_message_for_db="hello",
        queue=asyncio.Queue(),
        logger=main.logger,
    )

    assert seen["flag_present_at_turn_start"] is False, (
        "the previous Stop's flag was still set when the new turn began"
    )


@pytest.mark.asyncio
async def test_db_cancellation_row_is_still_cleared(wired):
    """The DB half of the same guard must not regress."""
    main, db, _bed = wired
    await _fakes._drive_stream(main, "sess-db-clear", "hello")
    db.clear_session_cancellation.assert_awaited()


# ---------------------------------------------------------------------------
# 2. A cancelled turn always leaves an assistant reply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_before_first_token_still_persists_an_assistant_turn(wired):
    """Otherwise the user's message disappears with no reply beside it —
    the turn looks like it never happened. The marker is also what carries
    the eval id and resume hint into the next turn's history."""
    import asyncio
    main, db, _bed = wired
    session_id = "sess-cancel-no-output"

    class _NoOutputAgent:
        async def run_conversation_turn_streaming(self, _msg):
            # Cancel arrives before the model emits anything.
            main.cancelled_sessions[session_id] = {"evalId": "eval_789"}
            yield {"type": "progress", "data": {"message": "working"}}
            await asyncio.sleep(0)

    queue: asyncio.Queue = asyncio.Queue()
    await main.run_agent_background(
        session_id=session_id,
        user_id="u1",
        agent=_NoOutputAgent(),
        final_message="run an eval",
        user_message_for_db="run an eval",
        queue=queue,
        logger=main.logger,
    )

    stored = await db.get_session_messages(session_id)
    assert [m["role"] for m in stored] == ["user", "assistant"], (
        f"a cancelled turn left the transcript unbalanced: {stored}"
    )
    assert "eval_789" in stored[-1]["content"], stored[-1]["content"]
    # Exactly one marker, not the marker plus a second appended suffix.
    assert stored[-1]["content"].count("cancelled by user") == 1, stored[-1]["content"]


# ---------------------------------------------------------------------------
# 3. What Bedrock actually rejects
#
# An earlier version of this fix also rewrote history to force strict
# user/assistant alternation, on the assumption that Bedrock rejects
# consecutive same-role turns. It does not. Probed live against
# us.anthropic.claude-sonnet-4-6 (the chat model) on 2026-09-22, via the
# same invoke_model + Anthropic Messages API path backend chat uses:
#
#   two consecutive user turns    -> ACCEPTED
#   two consecutive assistant     -> ACCEPTED
#   leading assistant turn        -> ACCEPTED
#   empty user content            -> REJECTED  ValidationException:
#                                    "user messages must have non-empty content"
#   whitespace-only content       -> REJECTED  ValidationException:
#                                    "text content blocks must contain
#                                     non-whitespace text"
#
# Converse accepts the non-alternating shapes too. So the constraint worth
# defending is non-empty content, not alternation — and that is already
# handled by get_session_messages' empty filter (see
# test_chat_reconnect_empty_message.py). These tests pin the real rule so
# nobody re-adds a history rewriter on the false premise.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_transcript_with_two_user_rows_is_replayed_as_is(wired):
    """No history rewriting. The double-user shape the cancel bug used to
    leave behind is passed to the model unchanged, because Bedrock accepts
    it. Pinning this keeps a 'repair' from being reintroduced."""
    main, db, bed = wired
    session_id = "sess-double-user"
    sent: list[list[dict]] = []

    class _RecordingBedrock(_fakes._ExplodingBedrock):
        async def create_message_streaming(self, messages, tools, system):
            sent.append([dict(m) for m in messages])
            self.called = True
            yield {"type": "text", "text": "ok"}
            yield {"type": "end", "stop_reason": "end_turn",
                   "response": {"content": [{"type": "text", "text": "ok"}]}}

    bed = _RecordingBedrock()
    main.bedrock_client = bed

    # The shape the live bug left behind (session 5911f319, reproduced
    # locally against the pre-fix backend on 2026-09-22).
    await db.save_message("m1", session_id, "user", "run a simple eval")
    await db.save_message("m2", session_id, "assistant", "Let me check models")
    await db.save_message("m3", session_id, "user", "use a tiny dataset")
    await db.save_message("m4", session_id, "user", "this is taking too long")

    chunks = await _fakes._drive_stream(main, session_id, "are you there?")

    assert "error" not in _fakes._collect(chunks), chunks
    assert bed.called, "the turn never reached the model"
    roles = [m["role"] for m in sent[0]]
    assert roles.count("user") == 4, roles
    assert roles[2] == "user" and roles[3] == "user", (
        f"history was rewritten; it should be replayed verbatim: {roles}"
    )


@pytest.mark.asyncio
async def test_the_cancel_marker_is_never_empty_or_whitespace(wired):
    """This is the constraint Bedrock does enforce. The marker persisted by
    a cancel-before-first-token becomes an assistant row that gets replayed
    on every later turn, so an empty or whitespace-only one would break the
    session for good."""
    main, _db, _bed = wired
    for info in ({}, {"evalId": None}, {"evalId": "eval_1"},
                 {"evalId": "eval_1", "configName": "cfg"}):
        marker = main._cancel_suffix(info)
        assert marker.strip(), f"empty marker for {info!r}"


@pytest.mark.asyncio
async def test_stop_during_a_tool_call_still_persists_an_assistant_turn(wired):
    """The other cancel branch. A Stop on the pod running the turn arrives
    as asyncio.CancelledError, not via the cancelled_sessions flag, and it
    had the same `if full_response:` gate. Users press Stop precisely when a
    tool is slow, so there is usually no assistant text yet — and the turn
    then vanished from the transcript. Caught by cancelling a live local run
    mid tool_call on 2026-09-22, which left rows [user, assistant, user]."""
    import asyncio
    main, db, _bed = wired
    session_id = "sess-stop-during-tool"

    class _ToolThenCancelAgent:
        async def run_conversation_turn_streaming(self, _msg):
            yield {"type": "tool_call", "data": {"name": "generate_qa_pairs"}}
            # Stop button: cancel_chat() calls task.cancel() on this pod.
            main.cancelled_sessions[session_id] = {"evalId": "eval_tool"}
            raise asyncio.CancelledError

    queue: asyncio.Queue = asyncio.Queue()
    with contextlib.suppress(asyncio.CancelledError):
        await main.run_agent_background(
            session_id=session_id,
            user_id="u1",
            agent=_ToolThenCancelAgent(),
            final_message="generate 12 QA pairs",
            user_message_for_db="generate 12 QA pairs",
            queue=queue,
            logger=main.logger,
        )

    stored = await db.get_session_messages(session_id)
    assert [m["role"] for m in stored] == ["user", "assistant"], (
        f"a Stop during a tool call left the user's message unanswered: {stored}"
    )
    assert stored[-1]["content"].strip(), "persisted an empty assistant row"
    assert "eval_tool" in stored[-1]["content"], stored[-1]["content"]
