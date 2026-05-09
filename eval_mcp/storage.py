"""Filesystem storage for eval-mcp.

All state lives in EVAL_MCP_HOME (default: ~/.eval-mcp/).
Plain JSON files — no database, no S3, no multi-tenant.
"""

import json
import os
import time
import hashlib
from pathlib import Path
from typing import Optional


def get_home() -> Path:
    home = Path(os.environ.get("EVAL_MCP_HOME", Path.home() / ".eval-mcp"))
    home.mkdir(parents=True, exist_ok=True)
    return home


def get_datasets_dir() -> Path:
    d = get_home() / "datasets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_judges_dir() -> Path:
    d = get_home() / "judges"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_configs_dir() -> Path:
    d = get_home() / "configs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_logs_dir() -> Path:
    d = get_home() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_documents_dir() -> Path:
    d = get_home() / "documents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_temp_dir() -> Path:
    d = get_home() / "temp"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ============================================================
# Datasets
# ============================================================

def save_dataset(name: str, tests: list) -> str:
    hash_json = json.dumps(tests, sort_keys=True)
    dataset_id = hashlib.sha256(hash_json.encode()).hexdigest()[:12]
    data = {
        "id": dataset_id,
        "name": name,
        "tests": tests,
        "created_at": int(time.time() * 1000),
    }
    path = get_datasets_dir() / f"{dataset_id}.json"
    path.write_text(json.dumps(data, indent=2))
    return dataset_id


def get_dataset_by_name(name: str) -> Optional[dict]:
    for f in get_datasets_dir().glob("*.json"):
        try:
            data = json.loads(f.read_text())
            if data.get("name") == name:
                return data
        except Exception:
            pass
    return None


def list_datasets() -> list[dict]:
    results = []
    for f in sorted(get_datasets_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text())
            results.append({
                "name": data.get("name", f.stem),
                "id": data.get("id", f.stem),
                "count": len(data.get("tests", [])),
                "created_at": data.get("created_at"),
            })
        except Exception:
            pass
    return results


# ============================================================
# Judges
# ============================================================

def save_judge(name: str, config: dict) -> str:
    judge_id = hashlib.sha256(name.encode()).hexdigest()[:12]
    data = {
        "id": judge_id,
        "name": name,
        "config": config,
        "created_at": int(time.time() * 1000),
    }
    path = get_judges_dir() / f"{judge_id}.json"
    path.write_text(json.dumps(data, indent=2))
    return judge_id


def get_judge_by_name(name: str) -> Optional[dict]:
    for f in get_judges_dir().glob("*.json"):
        try:
            data = json.loads(f.read_text())
            if data.get("name") == name:
                return data
        except Exception:
            pass
    return None


def list_judges() -> list[dict]:
    results = []
    for f in sorted(get_judges_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text())
            results.append({
                "name": data.get("name", f.stem),
                "id": data.get("id", f.stem),
                "domain": data.get("config", {}).get("domain", "general"),
                "criteria": [c["name"] for c in data.get("config", {}).get("criteria", [])],
            })
        except Exception:
            pass
    return results


# ============================================================
# Documents
# ============================================================

def list_documents() -> list[str]:
    docs_dir = get_documents_dir()
    return [str(f) for f in docs_dir.iterdir() if f.is_file()]
