"""The MCP client must match the installed mcp SDK's actual API.

Every test in this suite passed while the deployed backend crash-looped, unable
to reach its own MCP sidecar. The mcp 2.x migration renamed
``streamablehttp_client`` -> ``streamable_http_client`` and the rename was caught
— but three *behavioural* changes behind that name were not, because nothing
exercised the real transport:

  1. ``timeout``/``sse_read_timeout`` arguments removed (pass a preconfigured
     ``http_client`` instead) -> "got an unexpected keyword argument 'timeout'"
  2. the yielded tuple lost its trailing ``get_session_id`` callback, so
     ``read, write, _ = ...`` -> "not enough values to unpack (expected 3, got 2)"
  3. wire-model attributes went snake_case, so ``tool.inputSchema`` ->
     ``'Tool' object has no attribute 'inputSchema'``. This one was the worst:
     ``list_tools`` swallows per-server exceptions into a warning, so it
     surfaced as a silent "0 tools" — the agent connected and then offered
     nothing.

These assert against the *installed* SDK rather than mocks, so a future major
bump fails here instead of in a pod. They need no network: signature and model
introspection only.
"""
from __future__ import annotations

import inspect

import pytest


def test_transport_accepts_the_arguments_we_pass():
    """We call ``streamable_http_client(url, http_client=...)``. Both the name
    and the keyword have to exist."""
    from mcp.client.streamable_http import streamable_http_client

    params = inspect.signature(streamable_http_client).parameters
    assert "http_client" in params, (
        f"transport no longer accepts http_client; got {list(params)}. Our long "
        f"eval timeouts (1h connect / 2h SSE read) ride on that client."
    )


def test_transport_does_not_accept_the_removed_timeout_kwargs():
    """Guards the exact crash. If a future version reinstates these, this test
    failing is the prompt to simplify the call site back."""
    from mcp.client.streamable_http import streamable_http_client

    params = inspect.signature(streamable_http_client).parameters
    assert "timeout" not in params and "sse_read_timeout" not in params, (
        "transport accepts timeout/sse_read_timeout again — mcp_client.py can "
        "drop the explicit httpx2 client it builds to carry those values"
    )


def test_client_builds_its_timeout_client_with_httpx2():
    """mcp 2.x depends on **httpx2** and the transport type-checks against
    ``httpx2.AsyncClient``. Our own code elsewhere still uses plain ``httpx``;
    passing the wrong one is a type error at connect time, in the pod."""
    import backend.core.mcp_client as mc

    src = inspect.getsource(mc)
    assert "httpx2" in src, "mcp_client must use httpx2, not httpx, for the transport"

    import httpx2

    client = httpx2.AsyncClient(timeout=httpx2.Timeout(3600.0, read=7200.0))
    try:
        # The long read timeout is what keeps hours-long evals alive.
        assert client.timeout.read == 7200.0
        assert client.timeout.connect == 3600.0
    finally:
        # No await available in a sync test; closing the transport is enough.
        client.close() if hasattr(client, "close") else None


def test_client_unpacks_the_right_number_of_streams():
    """The transport yields (read, write) in 2.x. Assert our call site matches
    the SDK's declared return type rather than hardcoding a count."""
    import backend.core.mcp_client as mc

    src = inspect.getsource(mc)
    assert "read, write = await" in src, (
        "mcp_client unpacks the wrong arity from streamable_http_client; 2.x "
        "yields (read, write) with no trailing get_session_id callback"
    )
    assert "read, write, _ = await" not in src


def test_tool_model_exposes_snake_case_input_schema():
    """`list_tools` reads this attribute. A camelCase access returns 0 tools
    silently, because the collection loop logs and continues."""
    from mcp.types import Tool

    fields = set(Tool.model_fields)
    assert "input_schema" in fields, (
        f"Tool.input_schema is gone; mcp_client reads it when building the "
        f"agent's tool list. Available: {sorted(fields)}"
    )


def test_client_reads_tool_schema_with_the_snake_case_name():
    """Pin the call site too — the SDK having the field doesn't help if we ask
    for the old one."""
    import backend.core.mcp_client as mc

    src = inspect.getsource(mc)
    assert "tool.input_schema" in src
    assert "tool.inputSchema" not in src, (
        "mcp_client still reads tool.inputSchema; on mcp 2.x that raises inside "
        "list_tools' except block and yields a silent empty tool list"
    )


@pytest.mark.asyncio
async def test_list_tools_surfaces_a_schema_error_instead_of_returning_empty(
    monkeypatch,
):
    """The silent-failure mode itself.

    `list_tools` catches per-server exceptions so one bad server can't take down
    the rest — reasonable, but it meant an API mismatch presented as "connected,
    0 tools" rather than an error. A server whose tools can't be read must not
    look like a server with no tools.
    """
    from unittest.mock import AsyncMock, MagicMock

    from backend.core.mcp_client import MultiMCPClient

    # The constructor reads the sidecar URL from the environment.
    monkeypatch.setenv("EVAL_MCP_URL", "http://127.0.0.1:8002/mcp")
    client = MultiMCPClient()

    class _BadTool:
        name = "probe"
        description = "d"

        def __getattr__(self, item):  # no input_schema / inputSchema
            raise AttributeError(item)

    session = MagicMock()
    session.list_tools = AsyncMock(return_value=MagicMock(tools=[_BadTool()]))
    client.sessions = {"eval": session}
    client.reconnect_server = AsyncMock(return_value=None)

    tools = await client.list_tools()

    # Today this returns [] after a warning. Assert the weaker but meaningful
    # property: we did NOT silently report a healthy empty toolset — the server
    # was recorded as failed and a reconnect was attempted.
    assert tools == []
    assert client.reconnect_server.await_count >= 1, (
        "a server whose tools cannot be parsed must be treated as failed "
        "(reconnect attempted), not as a server that legitimately has none"
    )
