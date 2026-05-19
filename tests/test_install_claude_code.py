"""Tests for the Claude Code installer.

The installer shells out to ``claude mcp list`` (cheaper than
``mcp get`` which spawns the server for a health check) and
``claude mcp add``. We mock subprocess.run and pin the exact args
sent to the CLI — these are the part that breaks silently when
Claude Code rev'd its flags last time.
"""
from __future__ import annotations

import subprocess

import pytest

from eval_mcp.installers.claude_code import ClaudeCodeInstaller


class _MockCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture
def calls(monkeypatch):
    """Record every subprocess.run invocation and return its captured args."""
    recorded = []

    def fake_run(cmd, **kwargs):
        recorded.append(cmd)
        if cmd[:3] == ["claude", "mcp", "list"]:
            return _MockCompleted(stdout=recorded_state.get("list_stdout", ""))
        if cmd[:3] == ["claude", "mcp", "add"]:
            return _MockCompleted(returncode=recorded_state.get("add_rc", 0),
                                  stderr=recorded_state.get("add_stderr", ""))
        if cmd[:3] == ["claude", "mcp", "remove"]:
            return _MockCompleted()
        return _MockCompleted()

    recorded_state: dict = {}
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "eval_mcp.installers.claude_code.subprocess.run", fake_run
    )
    monkeypatch.setattr(
        "eval_mcp.installers.claude_code.shutil.which",
        lambda cmd: "/usr/bin/claude" if cmd == "claude" else None,
    )
    return recorded, recorded_state


def test_install_adds_server_with_correct_args(calls):
    recorded, _state = calls
    result = ClaudeCodeInstaller().install()
    assert result.status == "installed"
    add_call = next(c for c in recorded if c[:3] == ["claude", "mcp", "add"])
    assert add_call == [
        "claude", "mcp", "add", "eval", "-s", "user", "--",
        "uvx", "--from", "llm-evaluation-system", "eval-mcp",
    ]


def test_skips_when_already_registered(calls):
    recorded, state = calls
    state["list_stdout"] = (
        "sentry: https://mcp.sentry.dev/mcp - ✓ Connected\n"
        "eval: uvx --from llm-evaluation-system eval-mcp - ✓ Connected\n"
    )
    result = ClaudeCodeInstaller().install()
    assert result.status == "skipped"
    assert "force" in result.message
    # `claude mcp add` should NOT have run
    assert not any(c[:3] == ["claude", "mcp", "add"] for c in recorded)


def test_force_removes_then_re_adds(calls):
    recorded, state = calls
    state["list_stdout"] = "eval: uvx ... - ✓ Connected\n"
    result = ClaudeCodeInstaller().install(force=True)
    assert result.status == "replaced"
    # remove ran, then add ran
    cmds = [c[:4] for c in recorded if c[0] == "claude"]
    assert ["claude", "mcp", "remove", "eval"] in cmds
    assert any(c[:3] == ["claude", "mcp", "add"] for c in recorded)


def test_returns_failed_when_claude_cli_missing(monkeypatch):
    monkeypatch.setattr(
        "eval_mcp.installers.claude_code.shutil.which", lambda cmd: None
    )
    result = ClaudeCodeInstaller().install()
    assert result.status == "failed"
    assert "claude" in result.message.lower()


def test_returns_failed_when_add_exits_nonzero(calls):
    _recorded, state = calls
    state["add_rc"] = 2
    state["add_stderr"] = "scope 'user' not writable"
    result = ClaudeCodeInstaller().install()
    assert result.status == "failed"
    assert "exited 2" in result.message
    assert "scope" in result.message


def test_detect_true_when_claude_on_path(monkeypatch):
    monkeypatch.setattr(
        "eval_mcp.installers.claude_code.has_command",
        lambda cmd: cmd == "claude",
    )
    monkeypatch.setattr(
        "eval_mcp.installers.claude_code.has_dir", lambda p: False
    )
    assert ClaudeCodeInstaller().detect() is True


def test_detect_true_when_only_claude_dir_present(monkeypatch):
    monkeypatch.setattr(
        "eval_mcp.installers.claude_code.has_command", lambda cmd: False
    )
    monkeypatch.setattr(
        "eval_mcp.installers.claude_code.has_dir",
        lambda p: p == "~/.claude",
    )
    assert ClaudeCodeInstaller().detect() is True


def test_detect_false_when_neither_present(monkeypatch):
    monkeypatch.setattr(
        "eval_mcp.installers.claude_code.has_command", lambda cmd: False
    )
    monkeypatch.setattr(
        "eval_mcp.installers.claude_code.has_dir", lambda p: False
    )
    assert ClaudeCodeInstaller().detect() is False
