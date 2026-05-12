"""Tests for the agent-environment auto-detector.

The detector is the zero-prompt UX layer: given a path the user dropped on
us (file or directory), figure out where their venv lives, which file
holds the entry, which function to call, and whether OTel is installed.
Each detector returns None when the answer isn't unambiguous — the caller
decides whether to prompt or fall back.

No interactive prompts here; this module is pure introspection.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# find_venv_python — walk up looking for a venv
# ---------------------------------------------------------------------------


def test_find_venv_python_prefers_dotvenv_next_to_agent(tmp_path: Path):
    """Most common project layout: agent.py + .venv/ in the same directory.
    The detector should find .venv/bin/python without walking further.
    """
    from eval_mcp.agent_detect import find_venv_python

    (tmp_path / "agent.py").write_text("def run_agent(p): return p\n")
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").touch()
    (venv / "bin" / "python").chmod(0o755)

    found = find_venv_python(str(tmp_path / "agent.py"))
    assert found == str(venv / "bin" / "python")


def test_find_venv_python_walks_up_to_parent_dir(tmp_path: Path):
    """Common layout when the agent lives in a sub-package:
    project/.venv/ + project/src/myorg/agent.py. Detector walks up.
    """
    from eval_mcp.agent_detect import find_venv_python

    agent = tmp_path / "src" / "myorg" / "agent.py"
    agent.parent.mkdir(parents=True)
    agent.write_text("def run_agent(p): return p\n")
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").touch()
    (venv / "bin" / "python").chmod(0o755)

    found = find_venv_python(str(agent))
    assert found == str(venv / "bin" / "python")


def test_find_venv_python_accepts_alternate_names(tmp_path: Path):
    """Some teams use `venv/` or `env/` instead of `.venv/`. Detector
    tries each in priority order.
    """
    from eval_mcp.agent_detect import find_venv_python

    (tmp_path / "agent.py").write_text("def run_agent(p): return p\n")
    venv = tmp_path / "venv"  # no leading dot
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").touch()
    (venv / "bin" / "python").chmod(0o755)

    found = find_venv_python(str(tmp_path / "agent.py"))
    assert found == str(venv / "bin" / "python")


def test_find_venv_python_returns_none_when_nothing_found(tmp_path: Path):
    """No venv anywhere → None. Caller can prompt or fall back."""
    from eval_mcp.agent_detect import find_venv_python

    (tmp_path / "agent.py").write_text("def run_agent(p): return p\n")
    found = find_venv_python(str(tmp_path / "agent.py"))
    assert found is None


# ---------------------------------------------------------------------------
# find_entry_file — pick the agent's main file when user gives a directory
# ---------------------------------------------------------------------------


def test_find_entry_file_prefers_agent_py(tmp_path: Path):
    """When multiple plausible files exist, agent.py wins."""
    from eval_mcp.agent_detect import find_entry_file

    (tmp_path / "agent.py").write_text("def run_agent(p): return p\n")
    (tmp_path / "main.py").write_text("def main(): pass\n")

    found = find_entry_file(str(tmp_path))
    assert found == str(tmp_path / "agent.py")


def test_find_entry_file_falls_back_to_main_py(tmp_path: Path):
    """If agent.py doesn't exist, main.py is the next candidate."""
    from eval_mcp.agent_detect import find_entry_file

    (tmp_path / "main.py").write_text("def run_agent(p): return p\n")

    found = find_entry_file(str(tmp_path))
    assert found == str(tmp_path / "main.py")


def test_find_entry_file_returns_input_when_already_a_file(tmp_path: Path):
    """If the user already pointed at a .py file, return it unchanged —
    don't try to be clever and override their explicit choice.
    """
    from eval_mcp.agent_detect import find_entry_file

    f = tmp_path / "anything.py"
    f.write_text("def run_agent(p): return p\n")

    assert find_entry_file(str(f)) == str(f)


def test_find_entry_file_returns_none_when_dir_has_no_candidates(tmp_path: Path):
    """Empty directory or no plausible entry file → None."""
    from eval_mcp.agent_detect import find_entry_file

    assert find_entry_file(str(tmp_path)) is None


# ---------------------------------------------------------------------------
# find_entry_function — pick the agent's main callable
# ---------------------------------------------------------------------------


def test_find_entry_function_prefers_run_agent(tmp_path: Path):
    """When multiple plausible names exist, run_agent wins (it's our
    documented convention).
    """
    from eval_mcp.agent_detect import find_entry_function

    f = tmp_path / "agent.py"
    f.write_text(
        "def ask(p): return p\n"
        "def run_agent(p): return p\n"
        "def main(): pass\n"
    )
    assert find_entry_function(str(f)) == "run_agent"


def test_find_entry_function_falls_through_known_names(tmp_path: Path):
    """Priority list: run_agent → ask → chat → main → run.
    Picks the first one that's actually defined.
    """
    from eval_mcp.agent_detect import find_entry_function

    f = tmp_path / "agent.py"
    f.write_text("def chat(p): return p\ndef other(): pass\n")
    assert find_entry_function(str(f)) == "chat"


def test_find_entry_function_returns_none_when_no_match(tmp_path: Path):
    """Nothing matches the priority list → None. Caller prompts."""
    from eval_mcp.agent_detect import find_entry_function

    f = tmp_path / "agent.py"
    f.write_text("def something_weird(p): return p\n")
    assert find_entry_function(str(f)) is None


def test_find_entry_function_handles_syntax_errors(tmp_path: Path):
    """Don't crash on broken Python files; return None so caller can show
    a useful error.
    """
    from eval_mcp.agent_detect import find_entry_function

    f = tmp_path / "agent.py"
    f.write_text("def run_agent(p:\n  # broken\n")  # syntax error
    assert find_entry_function(str(f)) is None


# ---------------------------------------------------------------------------
# check_otel_installed — does the user's venv have what we need
# ---------------------------------------------------------------------------


def test_check_otel_installed_true_for_our_venv():
    """Our own .venv has the OTel packages installed (they're dev deps).
    This is the positive control — exercises the actual import path.
    """
    from eval_mcp.agent_detect import check_otel_installed

    assert check_otel_installed(sys.executable) is True


def test_check_otel_installed_false_when_missing(tmp_path: Path):
    """A python without OTel → returns False. Uses a tiny fake python
    script that always exits non-zero on the import check.
    """
    from eval_mcp.agent_detect import check_otel_installed

    # Build a python wrapper that always fails the import check
    fake = tmp_path / "fake_python"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)

    assert check_otel_installed(str(fake)) is False


# ---------------------------------------------------------------------------
# detect_agent — orchestrator that calls all of the above
# ---------------------------------------------------------------------------


def test_detect_agent_full_happy_path(tmp_path: Path):
    """Full happy path: directory with agent.py, .venv/, and a working
    python → detector returns a complete AgentDetection with file,
    function, and venv_python populated.

    (The otel_installed flag isn't asserted here because a freshly-made
    fake venv can't have OTel imports working — see the dedicated
    `check_otel_installed` tests above for that path.)
    """
    from eval_mcp.agent_detect import detect_agent

    (tmp_path / "agent.py").write_text("def run_agent(p): return p\n")
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").touch()
    (venv / "bin" / "python").chmod(0o755)

    result = detect_agent(str(tmp_path))
    assert result.agent_file == str(tmp_path / "agent.py")
    assert result.entry_function == "run_agent"
    assert result.venv_python == str(venv / "bin" / "python")


def test_detect_agent_reports_missing_otel(tmp_path: Path):
    """When venv exists but OTel isn't installed there, detect_agent
    surfaces that — the caller can show the pip install command.
    """
    from eval_mcp.agent_detect import detect_agent

    (tmp_path / "agent.py").write_text("def run_agent(p): return p\n")
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    # Fake python that always reports OTel as missing.
    (venv / "bin" / "python").write_text("#!/bin/sh\nexit 1\n")
    (venv / "bin" / "python").chmod(0o755)

    result = detect_agent(str(tmp_path))
    assert result.venv_python is not None
    assert result.otel_installed is False


# ---------------------------------------------------------------------------
# install_otel_in_venv — run pip install in the user's venv
# ---------------------------------------------------------------------------


def test_install_otel_in_venv_builds_correct_pip_command(tmp_path: Path, monkeypatch):
    """We must invoke <venv>/bin/python -m pip install on the 3 OTel
    packages. Anything else (uv pip, conda install, etc.) doesn't go
    into the venv we asked for.
    """
    from eval_mcp.agent_detect import install_otel_in_venv

    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\necho 'pip ok'\nexit 0\n")
    venv_python.chmod(0o755)

    captured = {}
    import subprocess
    real_run = subprocess.run

    def spy(cmd, *a, **kw):
        captured["cmd"] = list(cmd)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    result = install_otel_in_venv(str(venv_python))

    assert result.success is True
    assert captured["cmd"][:3] == [str(venv_python), "-m", "pip"]
    assert "install" in captured["cmd"]
    assert "opentelemetry-distro" in captured["cmd"]
    assert "opentelemetry-exporter-otlp-proto-http" in captured["cmd"]
    assert "opentelemetry-instrumentation-botocore" in captured["cmd"]


def test_install_otel_in_venv_returns_error_on_failure(tmp_path: Path):
    """pip install can fail (network, permissions, version conflicts).
    We surface the stderr so the user has something to debug from.
    """
    from eval_mcp.agent_detect import install_otel_in_venv

    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\necho 'simulated pip failure' >&2\nexit 1\n")
    venv_python.chmod(0o755)

    result = install_otel_in_venv(str(venv_python))
    assert result.success is False
    assert "simulated pip failure" in result.message


def test_install_otel_in_venv_rejects_missing_python():
    """A bogus venv_python path should fail fast with a useful message,
    not a cryptic OSError.
    """
    from eval_mcp.agent_detect import install_otel_in_venv

    result = install_otel_in_venv("/nonexistent/python")
    assert result.success is False
    assert "not found" in result.message.lower() or "no such" in result.message.lower()
