"""Auto-detect what we need about a user's agent layout.

The contract: user points at a file or directory. We figure out without
asking anything:
  - which .py file holds the entry point
  - which function inside it to call
  - which Python interpreter to run it under (their venv)
  - whether OTel is installed in that venv

Each function returns None when the answer isn't unambiguous; callers
decide whether to prompt or fall back. Nothing here is interactive.
"""

from __future__ import annotations

import ast
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Priority lists — order matters. First match wins.
_ENTRY_FILES = ("agent.py", "main.py", "app.py", "__main__.py")
_ENTRY_FUNCTIONS = ("run_agent", "ask", "chat", "main", "run")
_VENV_DIRS = (".venv", "venv", "env")


# ---------------------------------------------------------------------------
# venv detection — walk up looking for the common venv directories
# ---------------------------------------------------------------------------


def find_venv_python(agent_path: str, *, max_walk: int = 6) -> Optional[str]:
    """Return the path to a Python interpreter in the user's venv.

    Strategy: start from agent_path's directory, look for any of the
    standard venv names (.venv, venv, env). If none found, walk up to
    `max_walk` parent directories. Return None if nothing matches.
    """
    p = Path(agent_path).expanduser().resolve()
    start = p.parent if p.is_file() else p
    cur = start
    for _ in range(max_walk + 1):
        for name in _VENV_DIRS:
            cand = cur / name / "bin" / "python"
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


# ---------------------------------------------------------------------------
# entry-file detection — if user gave us a dir, pick the agent's main .py
# ---------------------------------------------------------------------------


def find_entry_file(agent_path: str) -> Optional[str]:
    """Return path to the agent's main .py file.

    If agent_path is already a .py file, return it unchanged. If a
    directory, look for the standard candidate names in priority order.
    """
    p = Path(agent_path).expanduser().resolve()
    if p.is_file() and p.suffix == ".py":
        return str(p)
    if not p.is_dir():
        return None
    for name in _ENTRY_FILES:
        cand = p / name
        if cand.is_file():
            return str(cand)
    return None


# ---------------------------------------------------------------------------
# entry-function detection — AST scan for a callable matching our priority
# ---------------------------------------------------------------------------


def find_entry_function(agent_file: str) -> Optional[str]:
    """Return the name of the agent's entry function in `agent_file`.

    Parses the file with ast (cheap, no import side effects) and looks
    for top-level `def` matching our priority list. Returns None on
    syntax errors or if no priority-list name is present.
    """
    try:
        source = Path(agent_file).read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return None

    defined = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in _ENTRY_FUNCTIONS:
        if name in defined:
            return name
    return None


# ---------------------------------------------------------------------------
# OTel presence check — does this python have the packages we need
# ---------------------------------------------------------------------------


_OTEL_IMPORT_CHECK = (
    "import opentelemetry.distro, "
    "opentelemetry.exporter.otlp.proto.http.trace_exporter, "
    "opentelemetry.instrumentation.botocore"
)


def check_otel_installed(python_path: str, *, timeout: float = 5.0) -> bool:
    """Return True if the given Python can import the 3 OTel packages we need.

    Runs as a subprocess so we don't pollute the harness's import table.
    Falls back to False on any error (missing python, timeout, import error).
    """
    try:
        result = subprocess.run(
            [python_path, "-c", _OTEL_IMPORT_CHECK],
            capture_output=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# ---------------------------------------------------------------------------
# Top-level orchestrator — one call returns everything the caller needs
# ---------------------------------------------------------------------------


@dataclass
class AgentDetection:
    """What detect_agent figured out. Fields are None when undetermined."""

    agent_file: Optional[str]
    entry_function: Optional[str]
    venv_python: Optional[str]
    otel_installed: bool


def detect_agent(agent_path: str) -> AgentDetection:
    """One call → everything we can determine without prompting."""
    agent_file = find_entry_file(agent_path)
    entry_function = find_entry_function(agent_file) if agent_file else None
    venv_python = find_venv_python(agent_path)
    otel_installed = check_otel_installed(venv_python) if venv_python else False
    return AgentDetection(
        agent_file=agent_file,
        entry_function=entry_function,
        venv_python=venv_python,
        otel_installed=otel_installed,
    )


# ---------------------------------------------------------------------------
# install_otel_in_venv — run pip install in the user's venv
# ---------------------------------------------------------------------------


_OTEL_PACKAGES = (
    "opentelemetry-distro",
    "opentelemetry-exporter-otlp-proto-http",
    "opentelemetry-instrumentation-botocore",
)


@dataclass
class InstallResult:
    """Outcome of installing OTel into the user's venv."""

    success: bool
    message: str


def install_otel_in_venv(
    venv_python: str,
    *,
    timeout: float = 120.0,
) -> InstallResult:
    """Install the 3 OTel packages into the user's venv via `<venv>/bin/python -m pip install`.

    Idempotent (pip install on an already-installed package is a no-op).
    The pip command runs against the user's venv only — never the harness
    venv, never a global location.
    """
    if not Path(venv_python).is_file():
        return InstallResult(
            success=False,
            message=f"Python interpreter not found at {venv_python}",
        )

    # Try the agent's own pip first (`python -m pip install`). Works for
    # poetry/pipenv/regular venvs. Falls back to `uv pip install` when
    # the venv has no pip installed (the default for uv-created venvs —
    # `uv venv` skips pip for speed).
    for command in (
        [venv_python, "-m", "pip", "install", *_OTEL_PACKAGES],
        ["uv", "pip", "install", "--python", venv_python, *_OTEL_PACKAGES],
    ):
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            # `uv` not on PATH — try the next strategy.
            continue
        except subprocess.TimeoutExpired:
            return InstallResult(
                success=False,
                message=f"install timed out after {timeout}s",
            )
        if result.returncode == 0:
            return InstallResult(
                success=True,
                message=f"Installed OTel packages into {venv_python}",
            )
        # `python -m pip` returns "No module named pip" when uv created
        # the venv without pip. Retry with `uv pip install` rather than
        # surface that as the final error.
        if "No module named pip" not in (result.stderr or ""):
            break

    return InstallResult(
        success=False,
        message=f"install failed: {result.stderr.strip() or result.stdout.strip()}",
    )
