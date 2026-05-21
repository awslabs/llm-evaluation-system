"""Multi-pod chat-history persistence — stateless agent build.

In a multi-replica backend with no sticky routing, turn N can land on
Pod-A and turn N+1 on Pod-B. The previous design cached `Agent`
instances in a per-pod dict (`session_agents`) and only loaded history
from the DB the first time the pod saw a session. The result was
silent divergence: each pod's cached agent only knew about the turns
that had landed on it, and the model started answering as if the
other pod's turns never happened.

User-visible symptom: "my favorite number is 15" on one pod, then
"what is my favorite number?" on the next pod returns "I don't know
yet." Confirmed in prod (session `a34b2ee3...`) before the fix.

Fix: drop the in-memory agent cache entirely. Build a fresh `Agent`
per turn, hydrated from DB-backed conversation history. The DB is the
only cross-pod-coherent store, so reading it on every turn keeps
every pod's view identical.

This test drives `run_agent_background` directly with a fake DB and a
fake Bedrock so we can inspect exactly what the third turn sends to
the model. If a future change reintroduces an in-memory history cache
that shadows the DB, this test fails.
"""
from __future__ import annotations

import asyncio
import copy
import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest


class _SharedDB:
    """In-memory stand-in for Postgres. One DB shared across all simulated
    pods so writes from any pod are visible to subsequent reads — same
    coherence guarantee real Postgres gives us."""

    def __init__(self):
        self._messages: dict[str, list[dict]] = {}
        self.create_user = AsyncMock(return_value=None)
        self.create_session = AsyncMock(return_value=None)
        self.clear_session_cancellation = AsyncMock(return_value=None)
        self.get_session_cancellation = AsyncMock(return_value=None)
        self.update_session_title = AsyncMock(return_value=None)

    async def save_message(self, msg_id, session_id, role, content):
        self._messages.setdefault(session_id, []).append(
            {"id": msg_id, "role": role, "content": content,
             "timestamp": datetime.datetime.now()}
        )

    async def get_session_messages(self, session_id):
        return [
            {"id": m["id"], "role": m["role"], "content": m["content"],
             "timestamp": m["timestamp"].isoformat()}
            for m in self._messages.get(session_id, [])
        ]


class _FakeBedrock:
    """Captures the `messages` list it was called with on each turn so
    the test can assert exactly what the model saw."""

    def __init__(self):
        self.calls: list[list[dict]] = []
        self.responses: list[str] = []

    def convert_mcp_tools_to_claude(self, tools):
        return []

    def extract_text_from_response(self, resp):
        if isinstance(resp, dict):
            for b in resp.get("content", []) or []:
                if isinstance(b, dict) and b.get("type") == "text":
                    return b.get("text", "")
        return ""

    def extract_tool_uses(self, resp):
        return []

    def create_tool_result_content(self, tu_id, r):
        return {"type": "tool_result", "tool_use_id": tu_id, "content": str(r)}

    async def create_message_streaming(self, messages, tools, system):
        self.calls.append(copy.deepcopy(messages))
        reply = self.responses.pop(0) if self.responses else "ok"
        yield {"type": "text", "text": reply}
        yield {
            "type": "end",
            "stop_reason": "end_turn",
            "response": {"content": [{"type": "text", "text": reply}]},
        }

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


@pytest.mark.asyncio
async def test_history_survives_three_turns_via_db_reload():
    """Three turns on the same session, each driving the production
    chat_stream path (fresh Agent, DB-hydrated history). Turn 3 must
    see turns 1 AND 2 in its conversation history. With the old
    cached-agent design, turn 3 on a pod that hadn't seen turn 2
    would miss it — this test catches any regression to that pattern.
    """
    from backend.api import main
    from backend.core.agent import Agent

    shared_db = _SharedDB()

    bed = _FakeBedrock()
    main.bedrock_client = bed
    main.mcp_client = _FakeMCP()
    main.db = shared_db
    main.cancelled_sessions.clear()
    main.active_tasks.clear()
    main.event_queues.clear()

    bed.responses = [
        "I don't know your favorite number yet.",
        "Got it — 15.",
        "Your favorite number is 15.",
    ]

    session_id = "session-xpod-bug"
    user_id = "user-xpod"

    async def drive_turn(user_msg):
        # Mirror chat_stream: build a fresh Agent, hydrate from DB, hand
        # it to run_agent_background. There's intentionally no per-pod
        # state — that's the whole point of the stateless model.
        existing = await main.db.get_session_messages(session_id)
        agent = Agent(main.bedrock_client, main.mcp_client, debug=False)
        agent.conversation_history = [
            {"role": m["role"], "content": m["content"]} for m in existing
        ]

        queue: asyncio.Queue = asyncio.Queue()
        main.event_queues[session_id] = queue
        task = asyncio.create_task(
            main.run_agent_background(
                session_id=session_id,
                user_id=user_id,
                agent=agent,
                final_message=user_msg,
                user_message_for_db=user_msg,
                queue=queue,
                logger=MagicMock(),
            )
        )
        while True:
            ev = await queue.get()
            if ev is None:
                break
        await task

    # Three turns. In the buggy prod code, the second and third turns
    # would have alternated pods (each pod with its own cached agent
    # diverging from the shared DB). In the stateless code, the "pod"
    # is irrelevant — every turn rebuilds the agent from DB.
    await drive_turn("what is my favorite number?")
    await drive_turn("its 15")
    await drive_turn("what is my favorite number?")

    assert len(bed.calls) == 3, f"expected 3 Bedrock calls, got {len(bed.calls)}"

    turn3_msgs = bed.calls[2]
    sent_flat = " | ".join(
        f"{m['role']}: {m['content'] if isinstance(m['content'], str) else '<blocks>'}"
        for m in turn3_msgs
    )

    # Turn 2's "its 15" must be present in what turn 3 sends to the model.
    user_contents = [
        m["content"] for m in turn3_msgs
        if m["role"] == "user" and isinstance(m["content"], str)
    ]
    assert "its 15" in user_contents, (
        f"Turn 3 (on Pod-A) lost turn 2's 'its 15' user message that "
        f"happened on Pod-B. This means session_agents caching is "
        f"shadowing the DB-backed history. Sent to Bedrock: {sent_flat}"
    )

    # Turn 2's assistant reply must be present too.
    assistant_contents = [
        m["content"] for m in turn3_msgs
        if m["role"] == "assistant" and isinstance(m["content"], str)
    ]
    assert any("15" in c or "Got it" in c for c in assistant_contents), (
        f"Turn 3 lost turn 2's assistant reply about '15'. "
        f"Sent to Bedrock: {sent_flat}"
    )
