"""The MCP server must actually import and start — not merely respond to --help.

This exists because mcp 2.0.0 removed `FastMCP` from `mcp.server` and shipped a
broken MCP in two consecutive releases (0.13.0, 0.14.0) without anything failing.
The dependency was pinned `mcp>=1.0.0`, so every fresh install resolved 2.0.0 and
died at `eval_mcp/server.py` with "cannot import name 'FastMCP'".

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


def test_fastmcp_is_importable():
    """Pin the specific symbol whose removal caused the outage.

    A dependency bump that takes FastMCP away must fail here — loudly, in CI —
    rather than in a user's IDE after release.
    """
    try:
        from mcp.server import FastMCP  # noqa: F401
    except ImportError as e:
        pytest.fail(
            "mcp.server.FastMCP is unavailable, so the MCP server cannot be "
            "constructed. mcp 2.x removed it in favour of MCPServer — either "
            "keep the `mcp<2` cap in pyproject.toml or migrate "
            f"eval_mcp/server.py to the 2.x API. Underlying error: {e}"
        )


def test_mcp_dependency_is_capped_below_2():
    """The declared requirement must exclude the incompatible major version.

    Asserted on package metadata rather than the pyproject text so it reflects
    what a user actually resolves.
    """
    import importlib.metadata as md

    reqs = md.requires("llm-evaluation-system") or []
    mcp_reqs = [r for r in reqs if r.split(";")[0].strip().startswith("mcp")]
    assert mcp_reqs, "no mcp requirement declared"
    assert any("<2" in r for r in mcp_reqs), (
        f"mcp must be capped below 2.0 until server.py migrates off FastMCP; "
        f"found {mcp_reqs}"
    )
