"""Kiro installer — JSON merge into ``~/.kiro/settings/mcp.json``."""
from __future__ import annotations

from pathlib import Path

from . import _json_merge
from .base import MCP_ARGS, MCP_COMMAND, SERVER_NAME, Result
from .detect import has_dir

CONFIG_PATH = Path("~/.kiro/settings/mcp.json").expanduser()


class KiroInstaller:
    name = "kiro"
    display = "Kiro"

    def detect(self) -> bool:
        return has_dir("~/.kiro")

    def install(self, *, force: bool = False) -> Result:
        server_config = {"command": MCP_COMMAND, "args": MCP_ARGS}
        try:
            status, backup = _json_merge.merge_mcp_server(
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
        return "Kiro picks up mcp.json changes automatically — no restart needed."
