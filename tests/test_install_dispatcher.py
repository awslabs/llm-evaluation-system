"""Tests for the ``eval-mcp install`` CLI dispatcher (eval_mcp/cli.py).

The dispatcher's job: detect IDEs, ask which to install into (or honor
flags), call each installer, print a summary, warm the uvx cache, and
print restart hints. We swap in fake installers via ``REGISTRY``
monkeypatching so these tests don't touch the network or the user's
real config files.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from eval_mcp import cli as cli_mod
from eval_mcp import installers as installers_mod
from eval_mcp.installers.base import Result


class FakeInstaller:
    """Drop-in replacement for a real installer. Records calls and
    returns a canned Result so we can assert on dispatcher behavior
    without touching real subprocess/filesystem."""

    def __init__(self, name, display, *, detected=True,
                 result_status="installed", result_message=""):
        self.name = name
        self.display = display
        self._detected = detected
        self._result = Result(display, result_status, result_message)
        self.install_calls: list[bool] = []  # records force= each time

    def detect(self) -> bool:
        return self._detected

    def install(self, *, force: bool = False) -> Result:
        self.install_calls.append(force)
        return self._result

    def restart_hint(self) -> str:
        return f"Restart {self.display}."


@pytest.fixture
def fake_registry(monkeypatch):
    """Build a registry with two detected + one undetected installer."""
    fakes = {
        "claude-code": FakeInstaller("claude-code", "Claude Code"),
        "kiro": FakeInstaller("kiro", "Kiro"),
        "codex": FakeInstaller("codex", "Codex", detected=False),
    }
    monkeypatch.setattr(installers_mod, "REGISTRY", fakes)
    # cli.py does `from eval_mcp.installers import REGISTRY` inside the
    # function, so the patched attribute is what gets picked up.
    monkeypatch.setattr(cli_mod, "_warm_uvx_cache", lambda: None)
    return fakes


def _invoke(args, input=None):
    return CliRunner().invoke(cli_mod.install, args, input=input)


def test_yes_installs_into_all_detected(fake_registry):
    result = _invoke(["--yes"])
    assert result.exit_code == 0, result.output
    assert fake_registry["claude-code"].install_calls == [False]
    assert fake_registry["kiro"].install_calls == [False]
    # not detected → not called
    assert fake_registry["codex"].install_calls == []
    # summary shown
    assert "Claude Code" in result.output
    assert "Kiro" in result.output
    # restart hints shown
    assert "Next steps:" in result.output


def test_ide_flag_honors_explicit_list(fake_registry):
    result = _invoke(["--ide", "claude-code", "--yes"])
    assert result.exit_code == 0
    assert fake_registry["claude-code"].install_calls == [False]
    assert fake_registry["kiro"].install_calls == []


def test_ide_flag_can_target_undetected_ide(fake_registry):
    """User passing --ide explicitly overrides detection — they know."""
    result = _invoke(["--ide", "codex", "--yes"])
    assert result.exit_code == 0
    assert fake_registry["codex"].install_calls == [False]


def test_unknown_ide_rejected(fake_registry):
    result = _invoke(["--ide", "emacs", "--yes"])
    assert result.exit_code != 0
    assert "emacs" in result.output


def test_force_flag_propagates(fake_registry):
    _invoke(["--ide", "claude-code", "--yes", "--force"])
    assert fake_registry["claude-code"].install_calls == [True]


def test_no_ides_detected_short_circuits(monkeypatch):
    monkeypatch.setattr(installers_mod, "REGISTRY", {
        "claude-code": FakeInstaller("claude-code", "Claude Code", detected=False),
    })
    monkeypatch.setattr(cli_mod, "_warm_uvx_cache", lambda: None)
    result = _invoke([])
    assert result.exit_code == 0
    assert "No supported IDEs detected" in result.output


def test_print_only_emits_install_md(fake_registry, monkeypatch):
    """Back-compat: --print-only still emits the bundled INSTALL.md
    and does NOT call any installer."""
    monkeypatch.setattr(cli_mod, "_print_install_guide", lambda: print("FAKE GUIDE"))
    result = _invoke(["--print-only"])
    assert result.exit_code == 0
    assert "FAKE GUIDE" in result.output
    assert fake_registry["claude-code"].install_calls == []


def test_failed_installer_does_not_abort_others(monkeypatch):
    fakes = {
        "claude-code": FakeInstaller(
            "claude-code", "Claude Code",
            result_status="failed", result_message="boom",
        ),
        "kiro": FakeInstaller("kiro", "Kiro"),
    }
    monkeypatch.setattr(installers_mod, "REGISTRY", fakes)
    monkeypatch.setattr(cli_mod, "_warm_uvx_cache", lambda: None)
    result = _invoke(["--yes"])
    assert result.exit_code == 0
    # both ran
    assert fakes["claude-code"].install_calls == [False]
    assert fakes["kiro"].install_calls == [False]
    # failure surfaced in the summary
    assert "failed" in result.output
    assert "boom" in result.output


def test_skipped_installs_do_not_show_restart_hint(monkeypatch):
    fakes = {
        "claude-code": FakeInstaller(
            "claude-code", "Claude Code",
            result_status="skipped", result_message="already present",
        ),
    }
    monkeypatch.setattr(installers_mod, "REGISTRY", fakes)
    monkeypatch.setattr(cli_mod, "_warm_uvx_cache", lambda: None)
    result = _invoke(["--yes"])
    assert result.exit_code == 0
    assert "skipped" in result.output
    # No "Next steps:" header when nothing was actually installed
    assert "Next steps:" not in result.output


def test_warm_cache_skipped_when_flag_set(monkeypatch):
    calls = []
    monkeypatch.setattr(installers_mod, "REGISTRY", {
        "claude-code": FakeInstaller("claude-code", "Claude Code"),
    })
    monkeypatch.setattr(cli_mod, "_warm_uvx_cache", lambda: calls.append("warm"))
    _invoke(["--yes", "--no-warm-cache"])
    assert calls == []


def test_warm_cache_runs_when_at_least_one_install_succeeded(monkeypatch):
    calls = []
    monkeypatch.setattr(installers_mod, "REGISTRY", {
        "claude-code": FakeInstaller("claude-code", "Claude Code"),
    })
    monkeypatch.setattr(cli_mod, "_warm_uvx_cache", lambda: calls.append("warm"))
    _invoke(["--yes"])
    assert calls == ["warm"]


def test_warm_cache_skipped_when_all_installs_failed_or_skipped(monkeypatch):
    calls = []
    monkeypatch.setattr(installers_mod, "REGISTRY", {
        "claude-code": FakeInstaller(
            "claude-code", "Claude Code", result_status="skipped"
        ),
    })
    monkeypatch.setattr(cli_mod, "_warm_uvx_cache", lambda: calls.append("warm"))
    _invoke(["--yes"])
    assert calls == []


def test_single_detected_skips_interactive_prompt(monkeypatch):
    """If only one IDE is detected, no need to ask which."""
    fakes = {
        "claude-code": FakeInstaller("claude-code", "Claude Code"),
        "kiro": FakeInstaller("kiro", "Kiro", detected=False),
    }
    monkeypatch.setattr(installers_mod, "REGISTRY", fakes)
    monkeypatch.setattr(cli_mod, "_warm_uvx_cache", lambda: None)
    # No --yes, no stdin: the single-detect short-circuit means we
    # don't deadlock on click.prompt.
    result = _invoke([])
    assert result.exit_code == 0
    assert fakes["claude-code"].install_calls == [False]


def test_interactive_quit(monkeypatch):
    fakes = {
        "claude-code": FakeInstaller("claude-code", "Claude Code"),
        "kiro": FakeInstaller("kiro", "Kiro"),
    }
    monkeypatch.setattr(installers_mod, "REGISTRY", fakes)
    monkeypatch.setattr(cli_mod, "_warm_uvx_cache", lambda: None)
    result = _invoke([], input="q\n")
    assert result.exit_code == 0
    assert fakes["claude-code"].install_calls == []
    assert fakes["kiro"].install_calls == []


def test_interactive_all_default(monkeypatch):
    """Hitting enter at the prompt installs into all detected."""
    fakes = {
        "claude-code": FakeInstaller("claude-code", "Claude Code"),
        "kiro": FakeInstaller("kiro", "Kiro"),
    }
    monkeypatch.setattr(installers_mod, "REGISTRY", fakes)
    monkeypatch.setattr(cli_mod, "_warm_uvx_cache", lambda: None)
    result = _invoke([], input="\n")
    assert result.exit_code == 0
    assert fakes["claude-code"].install_calls == [False]
    assert fakes["kiro"].install_calls == [False]


def test_interactive_explicit_subset(monkeypatch):
    fakes = {
        "claude-code": FakeInstaller("claude-code", "Claude Code"),
        "kiro": FakeInstaller("kiro", "Kiro"),
    }
    monkeypatch.setattr(installers_mod, "REGISTRY", fakes)
    monkeypatch.setattr(cli_mod, "_warm_uvx_cache", lambda: None)
    result = _invoke([], input="kiro\n")
    assert result.exit_code == 0
    assert fakes["claude-code"].install_calls == []
    assert fakes["kiro"].install_calls == [False]
