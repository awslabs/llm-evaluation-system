"""Pin the fix for the pod-shutdown-on-eval-cancel bug.

Background: anyio cancel scopes (used internally by the MCP
streamable-http client) are tied to the task that opened them.
Closing such a scope from a *different* task raises CancelledError on
the original opener. Under the previous design, the FastAPI lifespan
task opened the MCP connection, then a chat-cancel background task
(`_cancel_eval_subprocess_and_reconnect`) called `reconnect_server`
directly — which closed and reopened the exit stack from the wrong
task. anyio cancelled the lifespan, the cancel cascaded through the
lifespan's `yield`, and the pod shut down. Every eval cancel.

Fix: route reconnect signals through `_mcp_reconnect_queue`. A
dedicated `_mcp_owner_loop` task drains the queue and performs the
reconnect *in its own scope* — the same task that originally opened
the scope, so the close stays in-task.

This test asserts the wiring rather than the anyio scope mechanics
themselves (which would require a real MCP server to reproduce).
Specifically: after a successful eval cancel, the chat cancel cleanup
must NOT call `reconnect_server` directly. It must put a signal on
`_mcp_reconnect_queue` instead.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_eval_cancel_enqueues_reconnect_signal_not_direct_call(monkeypatch):
    """Eval cancel must NOT call mcp_client.reconnect_server directly —
    that's the cross-task scope close that bombs the pod. It must put
    a signal on _mcp_reconnect_queue for the owner task to drain.
    """
    from backend.api import main

    user_id = "test-user-eval-cancelled"
    monkeypatch.setenv("EVAL_MCP_URL", "http://localhost:8002/mcp")

    # Simulate "yes an eval was actually running" from the local MCP
    # /cancel endpoint — body says cancelled: True.
    class _CancelledResp:
        def json(self):
            return {"cancelled": True, "evalId": "e1", "configName": "c1"}

    class _FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, *a, **k):
            return _CancelledResp()

    monkeypatch.setattr(main, "httpx", MagicMock(AsyncClient=lambda: _FakeClient()))

    # Spy on mcp_client.reconnect_server — if the code calls it
    # directly the test fails, because that's the cross-task path
    # that crashed pods.
    direct_reconnect_called = asyncio.Event()
    fake_mcp = MagicMock()

    async def _direct_reconnect(*a, **k):
        direct_reconnect_called.set()

    fake_mcp.reconnect_server = _direct_reconnect
    monkeypatch.setattr(main, "mcp_client", fake_mcp)

    # Install a fresh queue and verify the function uses it.
    queue: asyncio.Queue = asyncio.Queue()
    monkeypatch.setattr(main, "_mcp_reconnect_queue", queue)

    await main._cancel_eval_subprocess_and_reconnect(user_id)

    # Direct call must NOT happen — that's the bug.
    assert not direct_reconnect_called.is_set(), (
        "_cancel_eval_subprocess_and_reconnect called "
        "mcp_client.reconnect_server directly. That closes the anyio "
        "scope from the wrong task and kills the pod. It should "
        "enqueue on _mcp_reconnect_queue instead."
    )

    # The signal must be on the queue, ready for the owner task.
    assert not queue.empty(), (
        "Eval cancel did not enqueue a reconnect signal. The MCP "
        "owner task will never know to refresh the session."
    )
    signal = queue.get_nowait()
    assert signal == "eval", (
        f"Expected 'eval' on the reconnect queue, got {signal!r}"
    )


@pytest.mark.asyncio
async def test_plain_chat_cancel_does_not_enqueue_reconnect(monkeypatch):
    """No eval was running → MCP /cancel returns cancelled: False →
    we must NOT enqueue a reconnect. Reconnecting for nothing burns
    the _reconnect_lock and slows the next message for no reason.
    """
    from backend.api import main

    user_id = "test-user-plain-cancel"
    monkeypatch.setenv("EVAL_MCP_URL", "http://localhost:8002/mcp")

    class _NoEvalResp:
        def json(self):
            return {"cancelled": False, "reason": "no running eval"}

    class _FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, *a, **k):
            return _NoEvalResp()

    monkeypatch.setattr(main, "httpx", MagicMock(AsyncClient=lambda: _FakeClient()))
    monkeypatch.setattr(main, "mcp_client", MagicMock())

    queue: asyncio.Queue = asyncio.Queue()
    monkeypatch.setattr(main, "_mcp_reconnect_queue", queue)

    await main._cancel_eval_subprocess_and_reconnect(user_id)

    assert queue.empty(), (
        "Plain-chat cancel enqueued a reconnect signal. That holds the "
        "_reconnect_lock and stalls the next user message for no reason."
    )


@pytest.mark.asyncio
async def test_owner_loop_drains_signal_in_its_own_task(monkeypatch):
    """The owner loop must execute reconnect_server in its own task
    scope. We can't test the anyio scope ownership directly without a
    real MCP server, but we can verify the call happens via the owner
    task — proving the architecture is right.
    """
    from backend.api import main

    seen_in_task: list[str] = []

    fake_mcp = MagicMock()

    async def _fake_connect():
        seen_in_task.append(f"connect:{asyncio.current_task().get_name()}")

    async def _fake_disconnect():
        seen_in_task.append(f"disconnect:{asyncio.current_task().get_name()}")

    async def _fake_reconnect(server_name, max_retries=10):
        seen_in_task.append(
            f"reconnect:{server_name}:{asyncio.current_task().get_name()}"
        )

    fake_mcp.connect = _fake_connect
    fake_mcp.disconnect = _fake_disconnect
    fake_mcp.reconnect_server = _fake_reconnect
    monkeypatch.setattr(main, "mcp_client", fake_mcp)

    connect_done = asyncio.Event()
    queue: asyncio.Queue = asyncio.Queue()

    owner = asyncio.create_task(
        main._mcp_owner_loop(connect_done, queue), name="mcp-owner"
    )

    await asyncio.wait_for(connect_done.wait(), timeout=2.0)

    # Push a reconnect signal — it should be processed by the owner.
    queue.put_nowait("eval")
    # Tiny delay for the owner to drain it.
    for _ in range(20):
        if any("reconnect:eval" in line for line in seen_in_task):
            break
        await asyncio.sleep(0.05)

    # Shut down the owner.
    queue.put_nowait(None)
    await asyncio.wait_for(owner, timeout=2.0)

    # All three operations must have run inside the owner task — that's
    # the structural invariant that prevents the cross-task scope bug.
    connect_lines = [s for s in seen_in_task if s.startswith("connect:")]
    reconnect_lines = [s for s in seen_in_task if s.startswith("reconnect:")]
    disconnect_lines = [s for s in seen_in_task if s.startswith("disconnect:")]

    assert connect_lines, f"Owner did not call connect. seen={seen_in_task}"
    assert reconnect_lines, f"Owner did not drain reconnect signal. seen={seen_in_task}"
    assert disconnect_lines, f"Owner did not disconnect on shutdown. seen={seen_in_task}"

    owner_name = "mcp-owner"
    assert all(owner_name in s for s in connect_lines + reconnect_lines + disconnect_lines), (
        f"Some MCP operation ran outside the owner task. seen={seen_in_task}"
    )
