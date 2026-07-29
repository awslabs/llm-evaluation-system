"""Reconnect must never start a turn with an empty message.

The frontend's `reconnectIfRunning` reattaches to a still-running answer
after a navigation or refresh: it checks `/api/chat/status/<id>` and, if
that says `running`, re-POSTs to `/api/chat/message` with an EMPTY
message to subscribe to the live stream.

That check is inherently racy — and the "running" flag can also be pure
debris, because `session_active` rows are only deleted by the finally
block of `run_agent_background`, which never runs if the pod is
OOM-killed or evicted mid-turn. Either way the empty POST arrives with
no live task to attach to.

Before this fix it fell through to the normal path and started a REAL
turn with empty text. Two consequences, both observed against the live
`make dev` stack:

1. Bedrock hard-rejects it —
   `ValidationException: messages.2: user messages must have non-empty
   content` — so the user sees a dead "Thinking…" bubble. Clicking
   History and returning stacked one more on every visit.

2. Far worse, the empty user message is persisted BEFORE the model call,
   so the session is poisoned permanently: every later turn re-hydrates
   that empty row from the DB and fails identically. The conversation
   can never be used again.

These tests pin both halves: the guard itself, and the TTL that stops a
dead pod's `session_active` row from luring the frontend into the guard
forever.
"""
from __future__ import annotations

import datetime
import json
from unittest.mock import AsyncMock, MagicMock

import pytest


class _RecordingDB:
    """Minimal in-memory DB that records what got written, so a test can
    prove an empty message never reached the `messages` table."""

    def __init__(self):
        self._messages: dict[str, list[dict]] = {}
        self.create_user = AsyncMock(return_value=None)
        self.create_session = AsyncMock(return_value=None)
        self.clear_session_cancellation = AsyncMock(return_value=None)
        self.get_session_cancellation = AsyncMock(return_value=None)
        self.update_session_title = AsyncMock(return_value=None)
        self.mark_session_active = AsyncMock(return_value=None)
        self.clear_session_active = AsyncMock(return_value=None)
        self.notify_session_event = AsyncMock(return_value=None)

    async def save_message(self, msg_id, session_id, role, content):
        self._messages.setdefault(session_id, []).append(
            {"id": msg_id, "role": role, "content": content,
             "timestamp": datetime.datetime.now()}
        )

    async def get_session_messages(self, session_id):
        # Mirrors the real query's `btrim(coalesce(content,'')) <> ''`
        # filter so a legacy empty row can't reach the model.
        return [
            {"id": m["id"], "role": m["role"], "content": m["content"],
             "timestamp": m["timestamp"].isoformat()}
            for m in self._messages.get(session_id, [])
            if (m["content"] or "").strip()
        ]

    async def raw_messages(self, session_id):
        """Unfiltered view, for asserting what's physically stored."""
        return list(self._messages.get(session_id, []))


class _ExplodingBedrock:
    """Stands in for Bedrock and fails the way the real service does if a
    turn is ever started with empty user content. If the guard works this
    is never called at all."""

    def __init__(self):
        self.called = False

    def convert_mcp_tools_to_claude(self, tools):
        return []

    def extract_text_from_response(self, resp):
        return ""

    def extract_tool_uses(self, resp):
        return []

    async def create_message_streaming(self, messages, tools, system):
        self.called = True
        for m in messages:
            if isinstance(m.get("content"), str) and not m["content"].strip():
                raise AssertionError(
                    "Bedrock was called with an empty user message — real "
                    "Bedrock answers this with ValidationException: "
                    "'user messages must have non-empty content'."
                )
        yield {"type": "text", "text": "ok"}
        yield {"type": "end", "stop_reason": "end_turn",
               "response": {"content": [{"type": "text", "text": "ok"}]}}

    def create_message(self, *a, **k):
        raise NotImplementedError


class _FakeMCP:
    def set_user_id(self, _u):
        pass

    async def list_tools(self):
        return []

    async def read_resource(self, _name):
        class _R:
            contents = [type("X", (), {"text": '{"tools": []}'})()]
        return _R()


def _collect(events: list[str]) -> list[str]:
    """Extract the SSE event names from raw stream chunks."""
    names = []
    for chunk in events:
        for line in chunk.splitlines():
            if line.startswith("event: "):
                names.append(line[len("event: "):].strip())
    return names


async def _drive_stream(main, session_id, message, user_id="u1"):
    request = main.ChatRequest(message=message, session_id=session_id, stream=True)
    return [chunk async for chunk in main.chat_stream(request, user_id)]


@pytest.fixture
def wired(monkeypatch):
    """Wire the module globals the streaming path reads."""
    from backend.api import main

    db = _RecordingDB()
    bed = _ExplodingBedrock()
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "bedrock_client", bed)
    monkeypatch.setattr(main, "mcp_client", _FakeMCP())
    main.cancelled_sessions.clear()
    main.active_tasks.clear()
    main.event_queues.clear()
    return main, db, bed


@pytest.mark.asyncio
async def test_empty_message_with_no_live_task_does_not_start_a_turn(wired):
    """The core guard: an empty POST with nothing running must close the
    stream instead of starting a turn."""
    main, db, bed = wired
    session_id = "sess-reconnect-race"

    chunks = await _drive_stream(main, session_id, "")

    assert not bed.called, (
        "An empty reconnect POST reached Bedrock. In production this "
        "returns ValidationException and leaves a dead 'Thinking…' bubble."
    )
    # The session announcement is fine (the frontend keys off it); what
    # must NOT appear is an error.
    assert "error" not in _collect(chunks)


@pytest.mark.asyncio
async def test_empty_message_is_never_persisted(wired):
    """The poisoning half. An empty user row in `messages` breaks the
    session forever, because every later turn re-hydrates it from the DB
    and Bedrock rejects the whole request."""
    main, db, bed = wired
    session_id = "sess-no-poison"

    # A real prior exchange, as if the user had already chatted.
    await db.save_message("m1", session_id, "user", "hey")
    await db.save_message("m2", session_id, "assistant", "Hey! How can I help?")

    await _drive_stream(main, session_id, "")

    # Assert on the RAW table, not the filtered read — the point is that
    # nothing was written, not merely that the read hides it.
    stored = await db.raw_messages(session_id)
    empties = [m for m in stored if not (m["content"] or "").strip()]
    assert not empties, (
        f"An empty message was persisted: {empties}. This permanently "
        f"poisons the session — every later turn reloads it and fails."
    )
    assert len(stored) == 2, f"expected the 2 original messages, got {stored}"


@pytest.mark.asyncio
async def test_already_poisoned_session_is_usable_again(wired):
    """Sessions poisoned before the guard shipped must recover.

    The guard prevents NEW empty rows but cannot remove the ones already
    in prod — including the session from the original bug report. Those
    stay broken forever unless the read filters blanks, because Bedrock
    rejects any history containing an empty user message.
    """
    main, db, bed = wired
    session_id = "sess-legacy-poison"

    # Exactly the shape the old bug left behind.
    await db.save_message("m1", session_id, "user", "hey")
    await db.save_message("m2", session_id, "assistant", "Hey!")
    await db.save_message("m3", session_id, "user", "")

    chunks = await _drive_stream(main, session_id, "are you there?")

    assert "error" not in _collect(chunks), (
        f"A session with a legacy empty row still fails: {chunks}"
    )
    assert bed.called, "the turn should have reached the model"
    # The empty row is still on disk; it just never reaches Bedrock.
    assert any(not (m["content"] or "").strip() for m in await db.raw_messages(session_id))


@pytest.mark.asyncio
async def test_session_still_usable_after_a_failed_reconnect(wired):
    """End-to-end proof the bug is gone: reconnect-race, then a normal
    message must work. Before the fix this second turn died with
    ValidationException because of the empty row left behind."""
    main, db, bed = wired
    session_id = "sess-recovers"

    await db.save_message("m1", session_id, "user", "hey")
    await db.save_message("m2", session_id, "assistant", "Hey!")

    # The racy reconnect.
    await _drive_stream(main, session_id, "")
    # A genuine follow-up. _ExplodingBedrock raises if history is dirty.
    chunks = await _drive_stream(main, session_id, "what is 2+2?")

    names = _collect(chunks)
    assert "error" not in names, (
        f"A normal message after a failed reconnect errored: {chunks}"
    )
    assert bed.called, "the real turn should have reached Bedrock"


@pytest.mark.asyncio
async def test_whitespace_only_message_is_also_rejected(wired):
    """`" "` is empty as far as Bedrock is concerned — guard on strip()."""
    main, db, bed = wired
    await _drive_stream(main, "sess-whitespace", "   \n\t ")
    assert not bed.called
    stored = await db.get_session_messages("sess-whitespace")
    assert stored == []


@pytest.mark.asyncio
async def test_stale_active_flag_is_cleared_by_the_guard(wired):
    """The guard clears `session_active` on its way out. Otherwise a dead
    pod's row keeps reporting `running: true` and every visit to the
    conversation fires another doomed reconnect."""
    main, db, bed = wired
    session_id = "sess-clears-flag"

    await _drive_stream(main, session_id, "")

    db.clear_session_active.assert_awaited_with(session_id)


@pytest.mark.asyncio
async def test_non_empty_message_still_starts_a_turn(wired):
    """Guard rail on the guard: normal traffic must be untouched."""
    main, db, bed = wired
    session_id = "sess-normal"

    chunks = await _drive_stream(main, session_id, "hello")

    assert bed.called, "a normal message must still reach the model"
    assert "error" not in _collect(chunks)
    stored = await db.get_session_messages(session_id)
    assert [m["role"] for m in stored] == ["user", "assistant"]
    assert stored[0]["content"] == "hello"


@pytest.mark.asyncio
async def test_non_stream_endpoint_rejects_empty_message(wired):
    """The JSON path shares the flaw and so shares the guard — it must
    reject rather than persist-then-fail."""
    from fastapi import HTTPException

    main, db, bed = wired
    request = main.ChatRequest(message="", session_id="sess-json", stream=False)

    with pytest.raises(HTTPException) as exc:
        await main.chat_non_stream(request, "u1")

    assert exc.value.status_code == 400
    assert not bed.called
    assert await db.get_session_messages("sess-json") == []


def test_get_session_messages_filters_empty_rows():
    """Sessions poisoned BEFORE the guard existed still carry an empty
    row, and it keeps breaking them: every turn re-hydrates that row and
    Bedrock rejects the request. The guard stops new poisoning but can't
    undo old damage, so the read filters blanks out — no migration, and
    it covers every consumer (agent hydration, /api/sessions, the title
    check) at once.

    Asserted at the SQL level because the filtering happens in the query.
    """
    import inspect

    from backend.core.database import Database

    src = inspect.getsource(Database.get_session_messages)
    assert "btrim" in src and "content" in src, (
        "get_session_messages no longer filters empty-content rows. "
        "Sessions poisoned before the empty-message guard shipped will "
        "stay permanently broken.\n"
        f"Current source:\n{src}"
    )


def test_session_active_query_is_ttl_bounded():
    """`session_active` rows are deleted in a finally block, which does
    not run when a pod is OOM-killed or evicted. Without a TTL on the
    read, such a row reports `running: true` forever and the frontend
    reconnects on every visit. Asserted at the SQL level because the
    behaviour lives in the query, not in Python.
    """
    import inspect

    from backend.core.database import Database

    src = inspect.getsource(Database.get_session_active)
    assert "started_at" in src and "interval" in src, (
        "get_session_active no longer bounds rows by age — a pod that dies "
        "mid-turn will leave a row that reports 'running' forever.\n"
        f"Current source:\n{src}"
    )
    # Must be a timedelta. asyncpg maps Postgres `interval` to
    # datetime.timedelta and rejects a str with a misleading
    # "'str' object has no attribute 'days'" — which get_session_active
    # catches and turns into `running: False` for EVERY session, silently
    # disabling cross-pod reconnect. Caught only by running it against a
    # real Postgres; a str sails through any mock.
    assert isinstance(Database.SESSION_ACTIVE_TTL, datetime.timedelta), (
        f"SESSION_ACTIVE_TTL must be a timedelta for asyncpg to bind it as "
        f"an interval, got {type(Database.SESSION_ACTIVE_TTL).__name__}"
    )
    # Long enough to cover a legitimately long turn (an eval can run for
    # a while); erring long is safe because the only cost of a false
    # 'running' is one harmless reconnect attempt, whereas erring short
    # would cut off a live stream.
    assert Database.SESSION_ACTIVE_TTL >= datetime.timedelta(hours=1)
