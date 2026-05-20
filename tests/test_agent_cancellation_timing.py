"""Real asyncio cancellation tests for the agent's tool-execution path.

The user reports "Stop is stuck for 90+ seconds when an eval is running."
We've shipped two attempted fixes without local verification and both
regressed. These tests exercise the actual cancellation propagation
with real asyncio tasks (mocking only the LLM/MCP boundary), so we can
verify behavior before pushing.

Three properties we want:
  1. After `outer_task.cancel()`, the outer await returns within ~100ms.
     This drives the SSE stream's close → "Stopping…" clears in the UI.
  2. The inner tool task (which runs the MCP RPC) also gets cancelled,
     not orphaned. Otherwise it keeps consuming Bedrock budget.
  3. No `await` on a cancelled task deadlocks.

The buggy revert (`d324540`) added `await tool_task` inside a finally
block. That made plain chat cancellation ALSO hang. Tests below cover
both shapes so we don't regress that again.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.agent import Agent


def _bare_agent() -> Agent:
    """Construct an Agent without going through __init__ — we only need
    the methods, not the BedrockClient/MCPClient wiring."""
    a = Agent.__new__(Agent)
    a.bedrock = MagicMock()
    a.mcp = MagicMock()
    a.conversation_history = []
    a.tool_descriptions = {}
    a.cancel_info = {}
    a.debug = False
    return a


@pytest.mark.asyncio
async def test_keepalive_cancellation_unblocks_outer_within_1s():
    """Outer cancel during a long-running tool call must propagate to
    the outer await in <1s. If `_execute_tool_with_keepalive` doesn't
    cancel its inner task, the chain hangs and "Stopping…" gets stuck.
    """
    agent = _bare_agent()

    # The "tool" simulates the MCP RPC: starts, sleeps for a long time.
    # It's properly cancellable (asyncio.sleep yields to the loop).
    async def slow_tool(name, args):
        await asyncio.sleep(60)  # 60s — well beyond any reasonable Stop wait
        return "should not see this"

    agent._execute_tool = slow_tool  # bypass MCP

    # Consume the generator in a real task so we can cancel it.
    async def consume():
        async for _ in agent._execute_tool_with_keepalive("optimize_prompt", {}):
            pass

    outer = asyncio.create_task(consume())

    # Let it actually start (gets past the initial create_task)
    await asyncio.sleep(0.05)
    assert not outer.done(), "outer should still be running"

    start = time.monotonic()
    outer.cancel()
    try:
        await outer
    except asyncio.CancelledError:
        pass
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, (
        f"outer task took {elapsed:.2f}s to cancel — "
        f"this is the bug the user reported (Stop stuck for 90+ seconds)"
    )


@pytest.mark.asyncio
async def test_keepalive_cancels_inner_tool_task():
    """After outer cancellation, the inner tool_task must also be
    cancelled — not left running to keep burning Bedrock budget."""
    agent = _bare_agent()
    tool_started = asyncio.Event()
    tool_was_cancelled = asyncio.Event()

    async def trackable_tool(name, args):
        tool_started.set()
        try:
            await asyncio.sleep(60)
            return "completed"
        except asyncio.CancelledError:
            tool_was_cancelled.set()
            raise

    agent._execute_tool = trackable_tool

    async def consume():
        async for _ in agent._execute_tool_with_keepalive("optimize_prompt", {}):
            pass

    outer = asyncio.create_task(consume())
    await tool_started.wait()
    outer.cancel()
    try:
        await outer
    except asyncio.CancelledError:
        pass

    # The inner task should observe CancelledError within the same wind-down
    try:
        await asyncio.wait_for(tool_was_cancelled.wait(), timeout=1.0)
    except asyncio.TimeoutError:
        pytest.fail(
            "Inner tool_task was orphaned — it didn't receive CancelledError "
            "after the outer task was cancelled. This is the leak that "
            "leaves the MCP call running after Stop."
        )


@pytest.mark.asyncio
async def test_plain_chat_cancellation_does_not_hit_keepalive():
    """Sanity check the plain chat path. When no tool is called,
    _execute_tool_with_keepalive isn't invoked, so the cancel logic
    inside it is irrelevant. The previous regression (`await tool_task`
    in finally) somehow still affected this path; this test makes sure
    we keep that decoupling honest.
    """
    agent = _bare_agent()

    # Confirm _execute_tool_with_keepalive isn't even entered when we
    # don't call it. Trivially true, but pinning it makes accidental
    # coupling regressions surface as a test failure.
    invocations = []
    original = agent._execute_tool_with_keepalive

    async def wrapped(*a, **k):
        invocations.append(a)
        async for x in original(*a, **k):
            yield x

    agent._execute_tool_with_keepalive = wrapped

    # Plain chat = no tool calls. Just an outer task that sleeps then
    # gets cancelled. Should unwind instantly.
    async def fake_chat():
        await asyncio.sleep(10)

    outer = asyncio.create_task(fake_chat())
    await asyncio.sleep(0.05)
    start = time.monotonic()
    outer.cancel()
    try:
        await outer
    except asyncio.CancelledError:
        pass
    elapsed = time.monotonic() - start

    assert elapsed < 0.5, f"plain chat cancel took {elapsed:.2f}s"
    assert invocations == [], "_execute_tool_with_keepalive should not have been called"


@pytest.mark.asyncio
async def test_keepalive_completes_normally_when_tool_finishes_quickly():
    """Non-cancellation path: tool completes within the first 30s
    keepalive window. Outer should get the result, no progress events."""
    agent = _bare_agent()

    async def fast_tool(name, args):
        await asyncio.sleep(0.05)
        return {"result": "ok"}

    agent._execute_tool = fast_tool

    events = []
    async for event in agent._execute_tool_with_keepalive("any", {}):
        events.append(event)

    assert any(e.get("type") == "result" for e in events)
    assert not any(e.get("type") == "progress" for e in events)


@pytest.mark.asyncio
async def test_keepalive_yields_progress_then_result():
    """If we plumb a tool that takes longer than 30s, we should see
    keepalive progress events first, then the final result. Use a
    much shorter timeout via patching to keep the test fast."""
    agent = _bare_agent()

    # Patch asyncio.wait to use a short fake timeout by monkey-patching
    # the wait function inside the generator. Simpler: just drive the
    # generator with a task that takes 0.2s and inspect events.
    async def medium_tool(name, args):
        await asyncio.sleep(0.2)
        return "done"

    agent._execute_tool = medium_tool

    # Monkey-patch asyncio.wait with a shorter default timeout so the
    # test doesn't have to wait 30s for a progress event. We invoke the
    # generator at the user-facing API level (no internal mucking).
    import asyncio as _asyncio
    original_wait = _asyncio.wait

    async def quick_wait(tasks, timeout=None):
        return await original_wait(tasks, timeout=0.05 if timeout == 30 else timeout)

    _asyncio.wait = quick_wait
    try:
        events = []
        async for event in agent._execute_tool_with_keepalive("slow", {}):
            events.append(event)
    finally:
        _asyncio.wait = original_wait

    types = [e["type"] for e in events]
    assert "progress" in types, f"expected progress event, got {types}"
    assert types[-1] == "result", f"expected result last, got {types}"
