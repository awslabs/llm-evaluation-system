"""Backup + atomic-write JSON config merger for IDEs that store MCP
config as JSON (Kiro, Cursor).

Pattern: read → parse → mutate → atomic write. Always make a
timestamped backup before the first write so users can recover if
something goes sideways.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def merge_mcp_server(
    config_path: Path | str,
    server_name: str,
    server_config: dict[str, Any],
    *,
    mcp_servers_key: str = "mcpServers",
    force: bool = False,
) -> tuple[str, str | None]:
    """Merge a single MCP server entry into ``config_path``.

    Returns ``(status, backup_path)`` where status is one of:

    - ``"installed"`` — new entry written
    - ``"skipped"`` — entry already present and ``force=False``
    - ``"replaced"`` — entry already present and ``force=True``

    Raises ``ValueError`` on malformed JSON or shape mismatch. Caller
    converts to a ``Result(status="failed", ...)``.
    """
    config_path = Path(config_path).expanduser()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        raw = config_path.read_text()
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as e:
            raise ValueError(f"{config_path} is not valid JSON: {e}") from e
    else:
        data = {}

    if not isinstance(data, dict):
        raise ValueError(f"{config_path} top-level is not a JSON object")

    servers = data.setdefault(mcp_servers_key, {})
    if not isinstance(servers, dict):
        raise ValueError(
            f"{config_path} has '{mcp_servers_key}' that is not an object"
        )

    if server_name in servers and not force:
        return "skipped", None

    status = "replaced" if server_name in servers else "installed"

    backup_path: str | None = None
    if config_path.exists():
        backup_path = f"{config_path}.bak.{int(time.time())}"
        Path(backup_path).write_text(config_path.read_text())

    servers[server_name] = server_config
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, config_path)
    return status, backup_path
