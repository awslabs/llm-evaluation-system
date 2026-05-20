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
