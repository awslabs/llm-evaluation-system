"""Cross-pod chat_status fix.

chat_status used to check only in-memory active_tasks, so a tab that
reconnects on a different pod always got {"running": false} even if the
agent was still running on the original pod. The fix: fall back to the
session_active DB table when the session isn't found locally.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import backend.api.main as main


# ---------------------------------------------------------------------------
# chat_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_local_task_running(monkeypatch):
    """Same-pod fast path: in-memory task present and running → True,
    no DB query issued."""
    task = asyncio.create_task(asyncio.sleep(100))
    monkeypatch.setattr(main, "active_tasks", {"sess-1": task})

    fake_db = AsyncMock()
    monkeypatch.setattr(main, "db", fake_db)

    result = await main.chat_status("sess-1", user_id="u1")
    assert result == {"running": True}
    fake_db.get_session_active.assert_not_called()
    task.cancel()


@pytest.mark.asyncio
async def test_status_not_local_hits_db_true(monkeypatch):
    """Cross-pod: session not in active_tasks on this pod → DB says running."""
    monkeypatch.setattr(main, "active_tasks", {})

    fake_db = AsyncMock()
    fake_db.get_session_active = AsyncMock(return_value=True)
    monkeypatch.setattr(main, "db", fake_db)

    result = await main.chat_status("sess-xpod", user_id="u1")
    assert result == {"running": True}
    fake_db.get_session_active.assert_awaited_once_with("sess-xpod")


@pytest.mark.asyncio
async def test_status_not_local_hits_db_false(monkeypatch):
    """Cross-pod: session not in active_tasks, DB says not running → False."""
    monkeypatch.setattr(main, "active_tasks", {})

    fake_db = AsyncMock()
    fake_db.get_session_active = AsyncMock(return_value=False)
    monkeypatch.setattr(main, "db", fake_db)

    result = await main.chat_status("sess-done", user_id="u1")
    assert result == {"running": False}


@pytest.mark.asyncio
async def test_status_db_timeout_returns_false(monkeypatch):
    """If the DB query times out the endpoint returns False, not an error."""
    monkeypatch.setattr(main, "active_tasks", {})

    async def slow_query(_sid):
        await asyncio.sleep(10)

    fake_db = AsyncMock()
    fake_db.get_session_active = slow_query
    monkeypatch.setattr(main, "db", fake_db)

    # Patch the timeout down to something instant so the test is fast.
    original_wait_for = asyncio.wait_for

    async def fast_timeout(coro, timeout):
        raise asyncio.TimeoutError

    with patch("asyncio.wait_for", side_effect=fast_timeout):
        result = await main.chat_status("sess-slow", user_id="u1")
    assert result == {"running": False}


@pytest.mark.asyncio
async def test_status_done_task_not_running(monkeypatch):
    """A task that has already completed should not count as running,
    even if it's still in the active_tasks dict (cleanup is async)."""
    task = asyncio.create_task(asyncio.sleep(0))
    await task  # let it complete

    monkeypatch.setattr(main, "active_tasks", {"sess-done": task})

    fake_db = AsyncMock()
    fake_db.get_session_active = AsyncMock(return_value=False)
    monkeypatch.setattr(main, "db", fake_db)

    result = await main.chat_status("sess-done", user_id="u1")
    assert result == {"running": False}


# ---------------------------------------------------------------------------
# mark / clear session_active lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_marks_active_and_clears_on_finish(monkeypatch):
    """run_agent_background must call mark_session_active at start and
    clear_session_active in its finally block regardless of outcome."""
    marked = []
    cleared = []

    async def fake_mark(sid, pod_id=""):
        marked.append(sid)

    async def fake_clear(sid):
        cleared.append(sid)

    fake_db = AsyncMock()
    fake_db.mark_session_active = fake_mark
    fake_db.clear_session_active = fake_clear
    fake_db.save_message = AsyncMock()
    fake_db.clear_session_cancellation = AsyncMock()
    fake_db.get_session_cancellation = AsyncMock(return_value=None)

    monkeypatch.setattr(main, "db", fake_db)
    monkeypatch.setattr(main, "active_tasks", {})
    monkeypatch.setattr(main, "event_queues", {})
    monkeypatch.setattr(main, "cancelled_sessions", {})

    import logging

    # Build a minimal fake agent that returns a single text chunk.
    fake_agent = AsyncMock()

    async def fake_run(message, stream=True):
        yield {"type": "text", "text": "hello"}

    fake_agent.run = fake_run

    queue: asyncio.Queue = asyncio.Queue()

    await main.run_agent_background(
        session_id="s1",
        user_id="u1",
        agent=fake_agent,
        final_message="hi",
        user_message_for_db="hi",
        queue=queue,
        logger=logging.getLogger("test"),
    )

    assert "s1" in marked, "mark_session_active not called"
    assert "s1" in cleared, "clear_session_active not called in finally"
