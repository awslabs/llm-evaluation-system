"""Codex CLI installer — TOML merge into ``~/.codex/config.toml``."""
from __future__ import annotations

from pathlib import Path

from . import _toml_merge
from .base import MCP_ARGS, MCP_COMMAND, SERVER_NAME, Result
from .detect import has_dir

CONFIG_PATH = Path("~/.codex/config.toml").expanduser()


class CodexInstaller:
    name = "codex"
    display = "Codex"

    def detect(self) -> bool:
        return has_dir("~/.codex")

    def install(self, *, force: bool = False) -> Result:
        server_config = {"command": MCP_COMMAND, "args": list(MCP_ARGS)}
        try:
            status, backup = _toml_merge.merge_mcp_server(
                CONFIG_PATH, SERVER_NAME, server_config, force=force
            )
        except ValueError as e:
            return Result(self.display, "failed", str(e))
        except OSError as e:
            return Result(self.display, "failed", f"write failed: {e}")

        if status == "skipped":
            return Result(
                self.display,
                "skipped",
                f"'{SERVER_NAME}' already in {CONFIG_PATH} (use --force to overwrite)",
            )
        msg = f"{CONFIG_PATH}"
        if backup:
            msg += f" (backup: {backup})"
        return Result(self.display, status, msg, backup_path=backup)

    def restart_hint(self) -> str:
        return "Restart the Codex CLI for the new MCP server to load."
