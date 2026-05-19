"""Cursor installer — JSON merge into ``~/.cursor/mcp.json``."""
from __future__ import annotations

from pathlib import Path

from . import _json_merge
from .base import MCP_ARGS, MCP_COMMAND, SERVER_NAME, Result
from .detect import has_dir

CONFIG_PATH = Path("~/.cursor/mcp.json").expanduser()


class CursorInstaller:
    name = "cursor"
    display = "Cursor"

    def detect(self) -> bool:
        return has_dir("~/.cursor")

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
        return "Restart Cursor (quit + reopen) so it picks up the new MCP server."
