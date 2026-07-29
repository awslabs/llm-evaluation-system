"""The MCP server must actually import and start — not merely respond to --help.

This exists because mcp 2.0.0 removed `FastMCP` from `mcp.server` and shipped a
broken MCP in two consecutive releases (0.13.0, 0.14.0) without anything failing.
The dependency was pinned `mcp>=1.0.0`, so every fresh install resolved 2.0.0 and
died at `eval_mcp/server.py` with "cannot import name 'FastMCP'".

server.py has since migrated to the 2.x API, so the version assertions here are
inverted from their original form — the hazard is now resolving mcp 1.x, which
lacks `MCPServer`. The boot checks themselves are unchanged and are the reason
the migration could be verified at all.

It went unnoticed because the obvious checks all route around the failure:

  - `eval-mcp --help` is a Click callback that returns BEFORE
    `eval_mcp.server` is imported, so it exits 0 on a completely broken install.
  - `eval-mcp view --help` / `serve --help` likewise.
  - A developer venv pinned to mcp 1.x never reproduces it.

So the only check that catches this class of breakage is one that imports the
server module and confirms the tools actually registered.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_server_module_imports():
    """`import eval_mcp.server` must succeed.

    This is the exact line that broke: `from mcp.server import FastMCP`. In a
    subprocess so a failure is an assertion rather than a collection error that
    takes down the rest of the suite.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import eval_mcp.server"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"eval_mcp.server failed to import — the MCP would not start.\n"
        f"{result.stderr}"
    )


def test_server_registers_tools():
    """Importing the server must produce a populated tool registry.

    Guards the case where the import succeeds against a future mcp API but the
    registration decorators silently no-op, which would present to users as an
    MCP that connects and then offers nothing.
    """
    code = (
        "import asyncio, eval_mcp.server as s;"
        "tools = asyncio.run(s.mcp.list_tools());"
        "print(len(tools))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, f"listing tools failed:\n{result.stderr}"
    count = int(result.stdout.strip().splitlines()[-1])
    # ~27 at the time of writing; assert a floor rather than an exact number so
    # adding a tool doesn't break the test, but wholesale loss does.
    assert count >= 20, f"expected the full tool set, got {count}"


def test_mcpserver_is_importable():
    """Pin the constructor symbol the server is built from.

    The v1 counterpart of this test pinned `FastMCP`, whose removal in mcp
    2.0.0 was the original outage. Now that server.py is on the 2.x API, the
    symbol to guard is `MCPServer` — a future rename must fail here, loudly in
    CI, rather than in a user's IDE after release.
    """
    try:
        from mcp.server import MCPServer  # noqa: F401
    except ImportError as e:
        pytest.fail(
            "mcp.server.MCPServer is unavailable, so the MCP server cannot be "
            f"constructed. Check the mcp changelog for a rename. Error: {e}"
        )


def test_mcp_dependency_requires_2x():
    """The declared requirement must floor at the major version we target.

    server.py uses the 2.x API (`MCPServer`, transport args on the run
    methods), which 1.x does not provide — so resolving 1.x would break the
    import just as surely as unpinned 2.x did before the migration. This is the
    mirror of the old `<2` cap: the hazard moved from "too new" to "too old".

    Asserted on package metadata rather than the pyproject text so it reflects
    what a user actually resolves.
    """
    import importlib.metadata as md

    reqs = md.requires("llm-evaluation-system") or []
    mcp_reqs = [r for r in reqs if r.split(";")[0].strip().startswith("mcp")]
    assert mcp_reqs, "no mcp requirement declared"
    assert any(">=2" in r.replace(" ", "") for r in mcp_reqs), (
        f"mcp must require >=2.0 now that server.py uses the 2.x API; "
        f"found {mcp_reqs}"
    )


def test_server_reports_a_version():
    """`serverInfo.version` must not be empty.

    v1's FastMCP derived the version automatically; MCPServer defaults it to
    "" and would silently report a blank version to every client. server.py
    passes it explicitly from package metadata — this guards that wiring.
    """
    code = (
        "import eval_mcp.server as s;"
        "print(s.mcp.version or '<empty>')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, f"reading version failed:\n{result.stderr}"
    reported = result.stdout.strip().splitlines()[-1]
    assert reported != "<empty>", (
        "MCPServer was constructed without an explicit version=, so clients "
        "see an empty serverInfo.version."
    )
