"""The chat SSE stream must never go silent for long.

Observed live (us-east-2, 2026-09-22): a two-model eval ran 7.5 minutes.
The agent's own "progress" heartbeat fires every 30s, but only while a
tool is executing — and even so the browser stopped receiving bytes and
the connection was cut ~4 minutes in, exactly 60s after the last event it
got (CloudFront's origin_read_timeout is 60s). The backend logged
CLIENT DISCONNECT, finished the run in the background, and the answer
was never delivered. The user saw a stale status and a "network error".

The fix is a keepalive emitted by `chat_stream` itself whenever the event
queue is quiet, independent of the agent. These tests pin that for the
primary loop and both reconnect loops, using the same fakes as
test_chat_reconnect_empty_message.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

# Reuse the streaming-path fakes without turning tests/ into a package.
_spec = importlib.util.spec_from_file_location(
    "_reconnect_fakes", Path(__file__).with_name("test_chat_reconnect_empty_message.py")
)
_fakes = importlib.util.module_from_spec(_spec)
sys.modules["_reconnect_fakes"] = _fakes
_spec.loader.exec_module(_fakes)


class _SlowBedrock(_fakes._ExplodingBedrock):
    """Takes longer than the keepalive interval to produce its first token."""

    def __init__(self, delay: float):
        super().__init__()
        self.delay = delay

    async def create_message_streaming(self, messages, tools, system):
        self.called = True
        await asyncio.sleep(self.delay)
        yield {"type": "text", "text": "ok"}
        yield {"type": "end", "stop_reason": "end_turn",
               "response": {"content": [{"type": "text", "text": "ok"}]}}


@pytest.fixture
def wired(monkeypatch):
    from backend.api import main
    db = _fakes._RecordingDB()
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "mcp_client", _fakes._FakeMCP())
    # Fast cadence so the test doesn't wait 15s per keepalive.
    monkeypatch.setattr(main, "SSE_KEEPALIVE_SECONDS", 0.05)
    main.cancelled_sessions.clear()
    main.active_tasks.clear()
    main.event_queues.clear()
    return main, db


def _keepalives(chunks: list[str]) -> int:
    return sum(1 for c in chunks if c.startswith(": "))


def _events(chunks: list[str]) -> list[str]:
    return _fakes._collect(chunks)


def test_keepalive_is_an_sse_comment():
    """Comment lines are the one frame type every SSE parser (spec, and our
    frontend's "event:"/"data:" line reader) ignores — it must stay one."""
    from backend.api import main
    assert main.SSE_KEEPALIVE.startswith(":")
    assert main.SSE_KEEPALIVE.endswith("\n\n")
    assert "event:" not in main.SSE_KEEPALIVE and "data:" not in main.SSE_KEEPALIVE


def test_keepalive_interval_is_below_the_shortest_hop_timeout():
    """CloudFront origin_read_timeout is 60s (infra/platform/cloudfront.tf);
    the ALB idle timeout is 120s. Stay well under the smaller with margin
    for a delayed flush."""
    from backend.api import main
    assert main.SSE_KEEPALIVE_SECONDS <= 30


@pytest.mark.asyncio
async def test_primary_stream_emits_keepalives_while_quiet(wired, monkeypatch):
    main, _db = wired
    monkeypatch.setattr(main, "bedrock_client", _SlowBedrock(delay=0.3))

    chunks = await _fakes._drive_stream(main, "sess-quiet", "hello")

    assert _keepalives(chunks) >= 3, chunks
    # And the real events still arrive intact around them.
    names = _events(chunks)
    assert names[0] == "session"
    assert "complete" in names
    assert "error" not in names


@pytest.mark.asyncio
async def test_no_keepalive_when_events_flow_promptly(wired, monkeypatch):
    """Keepalives fill silence, they don't pad a healthy stream."""
    main, _db = wired
    monkeypatch.setattr(main, "SSE_KEEPALIVE_SECONDS", 5.0)
    monkeypatch.setattr(main, "bedrock_client", _SlowBedrock(delay=0.0))

    chunks = await _fakes._drive_stream(main, "sess-fast", "hello")

    assert _keepalives(chunks) == 0, chunks
    assert "complete" in _events(chunks)


@pytest.mark.asyncio
async def test_same_pod_reconnect_emits_keepalives(wired, monkeypatch):
    """A tab reattaching to a run on this pod drains the in-memory queue
    and must stay warm while that queue is quiet too."""
    main, _db = wired
    monkeypatch.setattr(main, "bedrock_client", _SlowBedrock(delay=0.0))
    session_id = "sess-samepod"

    queue: asyncio.Queue = asyncio.Queue()
    main.event_queues[session_id] = queue

    async def _slow_producer():
        await asyncio.sleep(0.3)
        await queue.put({"type": "text", "data": {"content": "late"}})
        await queue.put(None)

    producer = asyncio.create_task(_slow_producer())
    main.active_tasks[session_id] = producer

    chunks = await _fakes._drive_stream(main, session_id, "")

    assert _keepalives(chunks) >= 3, chunks
    assert "text" in _events(chunks)


@pytest.mark.asyncio
async def test_cross_pod_reconnect_emits_keepalives(wired, monkeypatch):
    """Reattaching to a run on ANOTHER pod goes through LISTEN/NOTIFY; the
    quiet-path check there must also emit the keepalive."""
    from unittest.mock import AsyncMock, MagicMock
    main, db = wired
    monkeypatch.setattr(main, "bedrock_client", _SlowBedrock(delay=0.0))
    session_id = "sess-xpod"

    # Live task on "another pod": present in active_tasks, no local queue.
    never = asyncio.get_event_loop().create_future()
    holder = asyncio.create_task(asyncio.wait_for(never, timeout=5))
    main.active_tasks[session_id] = holder

    conn = MagicMock()
    conn.close = AsyncMock()
    conn.remove_listener = AsyncMock()
    conn.add_listener = AsyncMock()
    db.connect_for_listen = AsyncMock(return_value=conn)
    db._notify_channel = lambda sid: f"sess_{sid}"
    # Quiet for the first few polls, then the run is over.
    polls = {"n": 0}

    async def _active(_sid):
        polls["n"] += 1
        return polls["n"] < 4
    db.get_session_active = _active

    try:
        chunks = await _fakes._drive_stream(main, session_id, "")
    finally:
        holder.cancel()

    assert _keepalives(chunks) == 3, chunks
    assert conn.close.await_count == 1
