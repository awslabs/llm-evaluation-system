"""Tiny helpers shared across installers for IDE detection."""
from __future__ import annotations

import shutil
from pathlib import Path


def has_command(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def has_dir(path: str) -> bool:
    return Path(path).expanduser().is_dir()
