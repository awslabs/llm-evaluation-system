"""Backup + atomic-write TOML config merger for Codex.

Uses ``tomlkit`` so user comments and key order survive the round-trip.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import tomlkit


def merge_mcp_server(
    config_path: Path | str,
    server_name: str,
    server_config: dict[str, Any],
    *,
    table_prefix: str = "mcp_servers",
    force: bool = False,
) -> tuple[str, str | None]:
    """Merge ``[<table_prefix>.<server_name>]`` into ``config_path``.

    Returns ``(status, backup_path)`` with the same semantics as the
    JSON merger.
    """
    config_path = Path(config_path).expanduser()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        try:
            doc = tomlkit.parse(config_path.read_text())
        except Exception as e:
            raise ValueError(f"{config_path} is not valid TOML: {e}") from e
    else:
        doc = tomlkit.document()

    parent = doc.get(table_prefix)
    if parent is None:
        parent = tomlkit.table()
        doc[table_prefix] = parent

    if server_name in parent and not force:
        return "skipped", None
    status = "replaced" if server_name in parent else "installed"

    backup_path: str | None = None
    if config_path.exists():
        backup_path = f"{config_path}.bak.{int(time.time())}"
        Path(backup_path).write_text(config_path.read_text())

    entry = tomlkit.table()
    for k, v in server_config.items():
        entry[k] = v
    parent[server_name] = entry

    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text(tomlkit.dumps(doc))
    os.replace(tmp, config_path)
    return status, backup_path
