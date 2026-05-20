"""EKS cross-pod Stop-button bug.

On a single-pod backend, clicking Stop during a long eval kills the
Inspect subprocess fine — the cancel HTTP request lands on the same
pod that's running the eval, and `cancel_chat` POSTs to its local MCP
sidecar's `/cancel/{user_id}` endpoint.

In a multi-pod EKS deployment that path is silently incomplete. ALB
cookie stickiness sometimes fails through CloudFront, so the cancel
request lands on Pod-B while the eval is running on Pod-A. Pod-B's
local MCP sidecar has nothing registered in its in-memory
`_running_evaluations` dict — so its `/cancel/{user_id}` is a no-op.
Pod-A's agent loop sees the cross-pod cancel signal via the
`session_cancellations` DB poll, breaks out of its loop, and sends a
"cancelled" SSE event — but never tells its own MCP sidecar to SIGTERM
the Inspect subprocess. The eval keeps running for hours. The user
starts a new eval and now they're double-billed.

The fix: when the agent loop's DB-poll path detects a cancel, it must
ALSO call its own local `_cancel_eval_subprocess_and_reconnect(user_id)`
so the subprocess in *this* pod's sidecar actually dies. Safe to fire
unconditionally because the MCP cancel endpoint is a no-op when
nothing is registered (same-pod path runs it twice; second call is
harmless).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_cross_pod_cancel_fires_local_mcp_cancel(monkeypatch):
    """When the agent loop detects a cancel via the DB poll path
    (cross-pod case: cancel landed on a different pod), it MUST call
    its own `_cancel_eval_subprocess_and_reconnect` so the local MCP
    sidecar SIGTERMs the Inspect subprocess. Without this, Stop
    silently leaves long evals running on EKS.
    """
    from backend.api import main

    session_id = "test-cross-pod-session"
    user_id = "test-user-cross-pod"

    # Fake agent: yields one event so the cancel-check branch executes,
    # then blocks so the loop doesn't exit naturally before we observe
    # the cancel handling.
    async def fake_stream(*_args, **_kwargs):
        yield {"type": "text", "data": {"content": "starting eval..."}}
        await asyncio.sleep(60)
    fake_agent = MagicMock()
    fake_agent.run_conversation_turn_streaming = fake_stream

    main.session_agents[session_id] = fake_agent

    # Empty in-memory dict → forces the DB-poll branch (the cross-pod
    # detection path). This is what would happen on Pod-A when cancel
    # landed on Pod-B and only the DB row exists.
    main.cancelled_sessions.pop(session_id, None)

    fake_db = MagicMock()
    fake_db.save_message = AsyncMock(return_value=None)
    fake_db.clear_session_cancellation = AsyncMock(return_value=None)
    fake_db.get_session_messages = AsyncMock(return_value=[])
    fake_db.update_session_title = AsyncMock(return_value=None)
    fake_db.get_session_cancellation = AsyncMock(
        return_value={"cancelled_at": "now", "eval_info": '{"evalId":"e1","configName":"c1"}'}
    )
    monkeypatch.setattr(main, "db", fake_db)

    # Capture the local-MCP-cancel call. This is what the fix adds —
    # without it, this Event never fires and the eval subprocess on
    # this pod outlives the Stop button indefinitely.
    cancel_called = asyncio.Event()
    captured_user_id = {}

    async def fake_cancel(uid):
        captured_user_id["uid"] = uid
        cancel_called.set()

    monkeypatch.setattr(main, "_cancel_eval_subprocess_and_reconnect", fake_cancel)

    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(
        main.run_agent_background(
            session_id=session_id,
            user_id=user_id,
            final_message="run a long eval",
            user_message_for_db="run a long eval",
            queue=queue,
            logger=MagicMock(),
        )
    )

    try:
        try:
            await asyncio.wait_for(cancel_called.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            pytest.fail(
                "_cancel_eval_subprocess_and_reconnect was never called when "
                "the agent loop detected a cross-pod cancel via the DB poll. "
                "This is the EKS multi-pod Stop-button bug: the Inspect "
                "subprocess in this pod's MCP sidecar outlives the cancel "
                "forever, because only THIS pod can SIGTERM it but nothing "
                "in this pod knows to."
            )
        assert captured_user_id["uid"] == user_id, (
            f"local cancel called with wrong user_id: got "
            f"{captured_user_id['uid']!r}, expected {user_id!r}"
        )
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        main.session_agents.pop(session_id, None)
        main.cancelled_sessions.pop(session_id, None)
        main.active_tasks.pop(session_id, None)
        main.event_queues.pop(session_id, None)


@pytest.mark.asyncio
async def test_cross_pod_cancel_chat_writes_db_row_with_no_local_task(monkeypatch):
    """When the cancel HTTP request lands on the wrong pod (the task is
    on Pod-A but cancel hit Pod-B), cancel_chat MUST still write to
    session_cancellations so Pod-A's agent loop sees the cancel via DB
    poll. Otherwise the cross-pod signal never reaches the right pod
    and the SSE stream never closes — "Stopping…" frozen forever.

    The original c222fee fix added the DB write but left an early
    return above it (`if session_id not in active_tasks: return`), so
    the write was unreachable in the cross-pod case it was meant to
    fix. This test pins the corrected behavior.
    """
    from backend.api import main

    session_id = "test-cross-pod-no-local-task"
    user_id = "test-user-no-local"

    # Empty active_tasks → simulates this pod not running the task
    # (it's on another pod). This is the cross-pod case.
    main.active_tasks.pop(session_id, None)
    main.cancelled_sessions.pop(session_id, None)

    db_write_called = asyncio.Event()
    captured_session_id = {}

    async def fake_mark_cancelled(sid, eval_info_json=""):
        captured_session_id["sid"] = sid
        db_write_called.set()

    fake_db = MagicMock()
    fake_db.mark_session_cancelled = fake_mark_cancelled
    monkeypatch.setattr(main, "db", fake_db)

    # The MCP /eval-info call would normally go to localhost:8002.
    # On the wrong pod it returns {"running": False, ...}. Stub the
    # entire httpx flow so the test doesn't depend on a live MCP.
    class _FakeResp:
        def json(self):
            return {"running": False, "evalId": None, "configName": None}

    class _FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, *a, **k):
            return _FakeResp()
        async def post(self, *a, **k):
            return _FakeResp()

    monkeypatch.setattr(main, "httpx", MagicMock(AsyncClient=lambda: _FakeClient()))

    local_cancel_called = asyncio.Event()

    async def fake_local_cancel(uid):
        local_cancel_called.set()

    monkeypatch.setattr(main, "_cancel_eval_subprocess_and_reconnect", fake_local_cancel)

    # Stub the auth dependency so we can call cancel_chat directly.
    # cancel_chat is a FastAPI route; call the underlying function.
    response = await main.cancel_chat(session_id=session_id, user_id=user_id)

    try:
        await asyncio.wait_for(db_write_called.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pytest.fail(
            "cancel_chat did NOT write to session_cancellations when the "
            "task was on a different pod. The cross-pod signal is therefore "
            "never delivered, the agent loop never sees the cancel, and the "
            "SSE stream stays open forever. This is the 'Stopping… forever' "
            "bug on EKS."
        )

    assert captured_session_id["sid"] == session_id

    # The local MCP cancel must NOT fire on the wrong pod. The eval
    # subprocess and the chat agent are always co-located (the agent
    # calls its own pod's MCP via localhost), so a wrong-pod cancel
    # has nothing to kill locally — and firing the reconnect anyway
    # holds _reconnect_lock long enough that the user's next message
    # races into a "network error". The right pod's
    # run_agent_background DB-poll branch fires its own local cancel
    # when it sees the row we just wrote.
    await asyncio.sleep(0.1)  # give the (non-)task a tick to NOT fire
    assert not local_cancel_called.is_set(), (
        "cancel_chat fired local MCP cancel on the wrong pod — that's "
        "wasted work AND blocks the next message's list_tools on "
        "_reconnect_lock. Only fire local cancel when the task is local; "
        "the right pod will fire its own."
    )

    assert response.get("success") is True, (
        f"cancel_chat must return success when it signaled the cancel via DB, "
        f"even if no local task existed. Got: {response!r}"
    )
