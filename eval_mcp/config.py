"""Configuration management for eval-mcp.

Stores user settings in ~/.eval-mcp/config.json.
"""

import json
import os
from pathlib import Path
from typing import List, Optional

from eval_mcp.storage import get_home


def _config_path() -> Path:
    return get_home() / "config.json"


def get_config() -> dict:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def set_config_value(key: str, value: str):
    config = get_config()
    config[key] = value
    _config_path().write_text(json.dumps(config, indent=2))


def get_bucket() -> Optional[str]:
    return get_config().get("bucket")


def get_sync_region() -> Optional[str]:
    return get_config().get("region")


def get_user() -> str:
    configured = get_config().get("user")
    if configured:
        return configured

    env = os.environ.get("EVAL_MCP_USER")
    if env:
        return env

    # Auto-detect from AWS identity
    try:
        import boto3
        sts = boto3.client("sts")
        identity = sts.get_caller_identity()
        arn = identity["Arn"]
        # arn:aws:iam::123:user/alice → alice
        # arn:aws:sts::123:assumed-role/RoleName/alice → alice
        name = arn.split("/")[-1]
        return name
    except Exception:
        return "local"


def get_projects() -> List[str]:
    raw = get_config().get("projects", "")
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    return [p.strip() for p in raw.split(",") if p.strip()]
