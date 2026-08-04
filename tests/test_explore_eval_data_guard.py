"""`explore_eval_data` runs arbitrary Python via exec() and must be reachable
ONLY from the local, single-user stdio MCP — never the hosted/networked one.

A restricted-builtins exec is not a sandbox: attacker code can climb the object
graph through any passed-in helper's __globals__ (e.g. read_log.__globals__) to
recover os/open/__import__ without ever naming a blocked builtin. So the sound
boundary is refusing to run at all when the driver could be untrusted. The
hosted EKS deployment runs the MCP with EVAL_MCP_TRANSPORT=http
(helm/eval/templates/deployment.yaml); local IDE use is stdio.

These tests pin that guard so a future change can't silently re-expose an RCE
sink to the multi-tenant web app.
"""

from __future__ import annotations

import json

import pytest

from eval_mcp import server


@pytest.mark.asyncio
async def test_explore_eval_data_refuses_in_http_mode(monkeypatch):
    """Hosted mode (any non-stdio transport) must reject the tool before it
    executes a single line of the supplied code."""
    monkeypatch.setenv("EVAL_MCP_TRANSPORT", "http")

    # Code that would exfiltrate an env var if it ever ran.
    result = await server.explore_eval_data(
        user_id="attacker",
        code="import os; result = os.environ.get('DB_PASSWORD', 'ran')",
    )
    payload = json.loads(result)
    assert "error" in payload
    assert "disabled in hosted mode" in payload["error"]
    # The exfil code must not have executed.
    assert payload.get("result") is None
    assert payload["error"] != "ran"


@pytest.mark.asyncio
async def test_explore_eval_data_refuses_in_streamable_http_mode(monkeypatch):
    """Guard on transport != 'stdio', not on the literal 'http', so any future
    networked transport name is also refused."""
    monkeypatch.setenv("EVAL_MCP_TRANSPORT", "streamable-http")
    result = await server.explore_eval_data(user_id="x", code="result = 1")
    assert "disabled in hosted mode" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_explore_eval_data_empty_code_still_rejected(monkeypatch):
    """Empty code is rejected regardless of mode (sanity: the guard doesn't
    change the existing empty-input contract)."""
    monkeypatch.delenv("EVAL_MCP_TRANSPORT", raising=False)  # default stdio
    result = await server.explore_eval_data(user_id="x", code="")
    assert json.loads(result)["error"] == "code is required"


def test_hosted_deployment_sets_http_transport():
    """The guard's safety depends on the hosted MCP actually running in http
    mode. Pin the Helm env so a chart change that drops EVAL_MCP_TRANSPORT=http
    (which would silently re-enable the tool in prod) fails here instead."""
    from pathlib import Path

    chart = Path(__file__).resolve().parent.parent / "helm" / "eval" / "templates" / "deployment.yaml"
    text = chart.read_text()
    assert "EVAL_MCP_TRANSPORT" in text and '"http"' in text, (
        "helm deployment no longer sets EVAL_MCP_TRANSPORT=http for the MCP "
        "sidecar — explore_eval_data's hosted-mode guard would fail open."
    )
