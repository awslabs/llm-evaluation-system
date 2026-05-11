"""User storage helper for per-user file isolation.

Two storage backends:
- S3 (production): DATA_BUCKET env var set. JSON store lives in S3, eval logs
  written directly by Inspect AI to s3://{bucket}/users/{id}/logs/.
- Local filesystem (development): No DATA_BUCKET. Everything under USER_STORAGE_BASE.

Ephemeral files (task .py, temp dataset .json) always use local filesystem
via get_user_dir() — these are disposable and only needed during eval execution.
"""

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError

from eval_mcp.core.s3_client import (
    is_s3_enabled,
    get_document_content_from_s3,
    list_user_s3_documents,
)


# S3 data bucket for persistent user data (judges, datasets, configs, logs)
DATA_BUCKET = os.environ.get("DATA_BUCKET", "")

# AWS region
_AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")


def _get_s3_client():
    return boto3.client("s3", region_name=_AWS_REGION)


def _s3_enabled() -> bool:
    return bool(DATA_BUCKET)


# ============== User Directory (ephemeral local files) ==============


def get_user_base_dir() -> Path:
    """Get the base directory for ephemeral user storage."""
    base = os.environ.get("USER_STORAGE_BASE", "backend/users")
    return Path(base)


def safe_user_path(user_id: str, *parts: str) -> Path:
    """Resolve a path under the user's base directory, rejecting any traversal.

    All callers that build filesystem paths from user-derived input (user_id,
    group_id, filenames, config names) should go through this helper. Uses
    the OWASP-canonical ``os.path.realpath`` + ``startswith`` pattern:
    realpath resolves ``..`` segments *and* symlinks on both sides, and the
    startswith(base + os.sep) check confirms containment without the
    ``/var/data-evil`` sibling-prefix bypass. Also rejects user_ids with
    path separators or ``..``, and prevents `parts` from walking out of
    the user's own subtree even if the final path happens to land under
    base. Equivalent defense-in-depth to pathlib's resolve+is_relative_to,
    but written in the form CodeQL's ``py/path-injection`` rule recognizes.
    """
    if not user_id:
        raise ValueError("user_id is required")
    if '/' in user_id or '\\' in user_id or user_id in ('.', '..'):
        raise ValueError(f"invalid user_id: {user_id!r}")

    base_real = os.path.realpath(str(get_user_base_dir()))
    os.makedirs(base_real, exist_ok=True)

    user_root_real = os.path.realpath(os.path.join(base_real, user_id))
    if not (user_root_real == base_real or user_root_real.startswith(base_real + os.sep)):
        raise ValueError(f"path escape attempt (user_root): {user_root_real}")

    target_real = os.path.realpath(os.path.join(user_root_real, *parts))
    if not (target_real == user_root_real or target_real.startswith(user_root_real + os.sep)):
        raise ValueError(f"path escape attempt outside user root: {target_real}")

    return Path(target_real)


def get_user_dir(user_id: str) -> Path:
    """Get the local directory for a specific user's ephemeral files.

    In production, this is an emptyDir volume for temporary task files.
    In local dev, this is also where the JSON store and logs live.
    """
    user_dir = safe_user_path(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


def get_user_datasets_dir(user_id: str) -> Path:
    datasets_dir = get_user_dir(user_id) / "datasets"
    datasets_dir.mkdir(parents=True, exist_ok=True)
    return datasets_dir


def get_user_judges_dir(user_id: str) -> Path:
    judges_dir = get_user_dir(user_id) / "judges"
    judges_dir.mkdir(parents=True, exist_ok=True)
    return judges_dir


def get_user_configs_dir(user_id: str) -> Path:
    configs_dir = get_user_dir(user_id) / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    return configs_dir


def get_user_log_dir(user_id: str) -> str:
    """Get the log directory for a user's eval results.

    Returns an S3 URI in production, local path in development.
    """
    if _s3_enabled():
        return f"s3://{DATA_BUCKET}/users/{user_id}/logs"
    log_dir = safe_user_path(user_id, "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    return str(log_dir)


# ============== File save helpers (ephemeral, local only) ==============


def _try_replicate(filepath: Path, user_id: str) -> None:
    """Best-effort async replication to S3 (no-op if bucket isn't configured)."""
    try:
        from eval_mcp.s3_sync import replicate_async
        replicate_async(filepath, user_id=user_id)
    except Exception:
        pass


def save_dataset(user_id: str, filename: str, content: str) -> Path:
    safe_filename = os.path.basename(filename)
    if not safe_filename:
        raise ValueError("Invalid filename: empty after sanitization")
    filepath = get_user_datasets_dir(user_id) / safe_filename
    filepath.write_text(content)
    _try_replicate(filepath, user_id)
    return filepath


def save_judge(user_id: str, filename: str, content: str) -> Path:
    safe_filename = os.path.basename(filename)
    if not safe_filename:
        raise ValueError("Invalid filename: empty after sanitization")
    filepath = get_user_judges_dir(user_id) / safe_filename
    filepath.write_text(content)
    _try_replicate(filepath, user_id)
    return filepath


def save_config(user_id: str, filename: str, content: str) -> Path:
    safe_filename = os.path.basename(filename)
    if not safe_filename:
        raise ValueError("Invalid filename: empty after sanitization")
    filepath = get_user_configs_dir(user_id) / safe_filename
    filepath.write_text(content)
    _try_replicate(filepath, user_id)
    return filepath


def list_user_files(user_id: str, folder: str, pattern: str = "*") -> list:
    user_dir = get_user_dir(user_id)
    folder_path = user_dir / folder
    if not folder_path.exists():
        return []
    return list(folder_path.glob(pattern))


# ============== JSON Store (S3 in production, local in dev) ==============


def _s3_store_prefix(user_id: str, store_type: str) -> str:
    return f"users/{user_id}/store/{store_type}/"


def _get_json_store_dir(user_id: str, store_type: str) -> Path:
    """Get local JSON store directory (used only when S3 is not configured)."""
    safe_type = os.path.basename(store_type)
    if not safe_type or safe_type != store_type:
        raise ValueError(f"Invalid store_type: {store_type!r}")
    store_dir = safe_user_path(user_id, "store", safe_type)
    store_dir.mkdir(parents=True, exist_ok=True)
    return store_dir


def _generate_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _ensure_under_base(path: Path) -> Path:
    """Verify `path` stays within get_user_base_dir() using realpath+startswith."""
    base_real = os.path.realpath(str(get_user_base_dir()))
    resolved = os.path.realpath(str(path))
    if not (resolved == base_real or resolved.startswith(base_real + os.sep)):
        raise ValueError(f"path escape attempt: {resolved}")
    return Path(resolved)


def _load_json_file(path: Path) -> Optional[dict[str, Any]]:
    safe = _ensure_under_base(path)
    if not safe.exists():
        return None
    return json.loads(safe.read_text())


def _save_json_file(path: Path, data: dict[str, Any], user_id: Optional[str] = None) -> None:
    safe = _ensure_under_base(path)
    safe.write_text(json.dumps(data, indent=2))
    if user_id:
        _try_replicate(safe, user_id)


def _list_json_files(directory: Path) -> list[dict[str, Any]]:
    entries = []
    if not directory.exists():
        return entries
    for f in directory.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            entries.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    entries.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return entries


# --- S3 JSON store operations ---


def _s3_save_json(user_id: str, store_type: str, filename: str, data: dict[str, Any]) -> None:
    key = f"{_s3_store_prefix(user_id, store_type)}{filename}"
    _get_s3_client().put_object(
        Bucket=DATA_BUCKET,
        Key=key,
        Body=json.dumps(data, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


def _s3_load_json(user_id: str, store_type: str, filename: str) -> Optional[dict[str, Any]]:
    key = f"{_s3_store_prefix(user_id, store_type)}{filename}"
    try:
        response = _get_s3_client().get_object(Bucket=DATA_BUCKET, Key=key)
        return json.loads(response["Body"].read().decode("utf-8"))
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return None
        raise


def _s3_list_json(user_id: str, store_type: str) -> list[dict[str, Any]]:
    prefix = _s3_store_prefix(user_id, store_type)
    s3 = _get_s3_client()
    entries = []

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=DATA_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".json"):
                continue
            try:
                response = s3.get_object(Bucket=DATA_BUCKET, Key=obj["Key"])
                data = json.loads(response["Body"].read().decode("utf-8"))
                entries.append(data)
            except (json.JSONDecodeError, ClientError):
                continue

    entries.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return entries


def _s3_delete_json(user_id: str, store_type: str, filename: str) -> bool:
    key = f"{_s3_store_prefix(user_id, store_type)}{filename}"
    try:
        _get_s3_client().delete_object(Bucket=DATA_BUCKET, Key=key)
        return True
    except ClientError:
        return False


# ============== Judges ==============


def save_judge_to_db(user_id: str, name: str, config: dict[str, Any]) -> str:
    judge_id = _generate_id("judge")
    now = int(datetime.now().timestamp() * 1000)

    data = {
        "id": judge_id,
        "name": name,
        "type": "judge",
        "config": config,
        "created_at": now,
        "updated_at": now,
    }

    if _s3_enabled():
        _s3_save_json(user_id, "judges", f"{judge_id}.json", data)
    else:
        store_dir = _get_json_store_dir(user_id, "judges")
        _save_json_file(store_dir / f"{judge_id}.json", data, user_id)

    return judge_id


def get_judge_from_db(user_id: str, judge_id: str) -> Optional[dict[str, Any]]:
    if _s3_enabled():
        data = _s3_load_json(user_id, "judges", f"{judge_id}.json")
    else:
        store_dir = _get_json_store_dir(user_id, "judges")
        data = _load_json_file(store_dir / f"{judge_id}.json")

    if data and data.get("type") == "judge":
        return {
            "id": data["id"],
            "name": data["name"],
            "config": data["config"],
            "created_at": data["created_at"],
        }
    return None


def get_judge_by_name(user_id: str, name: str) -> Optional[dict[str, Any]]:
    if _s3_enabled():
        entries = _s3_list_json(user_id, "judges")
    else:
        store_dir = _get_json_store_dir(user_id, "judges")
        entries = _list_json_files(store_dir)

    for entry in entries:
        if entry.get("name") == name and entry.get("type") == "judge":
            return {
                "id": entry["id"],
                "name": entry["name"],
                "config": entry["config"],
                "created_at": entry["created_at"],
            }
    return None


def list_judges_from_db(user_id: str, search_term: str = "") -> list[dict[str, Any]]:
    if _s3_enabled():
        entries = _s3_list_json(user_id, "judges")
    else:
        store_dir = _get_json_store_dir(user_id, "judges")
        entries = _list_json_files(store_dir)

    results = []
    for entry in entries:
        if entry.get("type") != "judge":
            continue
        if search_term and search_term.lower() not in entry.get("name", "").lower():
            continue
        results.append({
            "id": entry["id"],
            "name": entry["name"],
            "config": entry["config"],
            "created_at": entry["created_at"],
        })
    return results


def delete_judge_from_db(user_id: str, judge_id: str) -> bool:
    if _s3_enabled():
        return _s3_delete_json(user_id, "judges", f"{judge_id}.json")
    store_dir = _get_json_store_dir(user_id, "judges")
    path = store_dir / f"{judge_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False


# ============== Eval Configs ==============


def save_eval_config_to_db(user_id: str, name: str, config: dict[str, Any]) -> str:
    config_id = _generate_id("eval")
    now = int(datetime.now().timestamp() * 1000)

    data = {
        "id": config_id,
        "name": name,
        "type": "eval",
        "config": config,
        "created_at": now,
        "updated_at": now,
    }

    if _s3_enabled():
        _s3_save_json(user_id, "eval_configs", f"{config_id}.json", data)
    else:
        store_dir = _get_json_store_dir(user_id, "eval_configs")
        _save_json_file(store_dir / f"{config_id}.json", data, user_id)

    return config_id


def get_eval_config_from_db(user_id: str, config_id: str) -> Optional[dict[str, Any]]:
    if _s3_enabled():
        data = _s3_load_json(user_id, "eval_configs", f"{config_id}.json")
    else:
        store_dir = _get_json_store_dir(user_id, "eval_configs")
        data = _load_json_file(store_dir / f"{config_id}.json")

    if data and data.get("type") == "eval":
        return {
            "id": data["id"],
            "name": data["name"],
            "config": data["config"],
            "created_at": data["created_at"],
        }
    return None


def list_eval_configs_from_db(user_id: str, search_term: str = "") -> list[dict[str, Any]]:
    if _s3_enabled():
        entries = _s3_list_json(user_id, "eval_configs")
    else:
        store_dir = _get_json_store_dir(user_id, "eval_configs")
        entries = _list_json_files(store_dir)

    results = []
    for entry in entries:
        if entry.get("type") != "eval":
            continue
        if search_term and search_term.lower() not in entry.get("name", "").lower():
            continue
        results.append({
            "id": entry["id"],
            "name": entry["name"],
            "config": entry["config"],
            "created_at": entry["created_at"],
        })
    return results


# ============== Datasets ==============


def save_dataset_to_db(user_id: str, name: str, tests: list[dict[str, Any]]) -> str:
    import hashlib

    hash_json = json.dumps(tests, sort_keys=True)
    dataset_id = hashlib.sha256(hash_json.encode()).hexdigest()
    now = int(datetime.now().timestamp() * 1000)

    data = {
        "id": dataset_id,
        "name": name,
        "type": "dataset",
        "tests": tests,
        "created_at": now,
    }

    if _s3_enabled():
        _s3_save_json(user_id, "datasets", f"{dataset_id}.json", data)
    else:
        store_dir = _get_json_store_dir(user_id, "datasets")
        _save_json_file(store_dir / f"{dataset_id}.json", data, user_id)

    return dataset_id


def get_dataset_from_db(user_id: str, dataset_id: str) -> Optional[dict[str, Any]]:
    if _s3_enabled():
        data = _s3_load_json(user_id, "datasets", f"{dataset_id}.json")
    else:
        store_dir = _get_json_store_dir(user_id, "datasets")
        data = _load_json_file(store_dir / f"{dataset_id}.json")

    if data:
        return {
            "id": data["id"],
            "tests": data["tests"],
            "created_at": data["created_at"],
        }
    return None


def get_dataset_by_name(user_id: str, name: str) -> Optional[dict[str, Any]]:
    if _s3_enabled():
        entries = _s3_list_json(user_id, "datasets")
    else:
        store_dir = _get_json_store_dir(user_id, "datasets")
        entries = _list_json_files(store_dir)

    for entry in entries:
        if entry.get("name") == name:
            return {
                "id": entry["id"],
                "name": entry["name"],
                "tests": entry["tests"],
                "created_at": entry["created_at"],
            }
    return None


def list_datasets_from_db(user_id: str, search_term: str = "") -> list[dict[str, Any]]:
    if _s3_enabled():
        entries = _s3_list_json(user_id, "datasets")
    else:
        store_dir = _get_json_store_dir(user_id, "datasets")
        entries = _list_json_files(store_dir)

    results = []
    for entry in entries:
        name = entry.get("name", "")
        if search_term and search_term.lower() not in name.lower():
            continue
        tests = entry.get("tests", [])
        results.append({
            "id": entry["id"],
            "name": name,
            "tests": tests,
            "num_samples": len(tests) if isinstance(tests, list) else 0,
            "created_at": entry["created_at"],
        })
    return results


# ============== Eval Results (pre-computed JSON for fast reads) ==============


def save_eval_groups(user_id: str, data: dict[str, Any]) -> None:
    """Save pre-computed groups response for a user (overwrites previous)."""
    if _s3_enabled():
        _s3_save_json(user_id, "eval_results", "_groups.json", data)
    else:
        store_dir = _get_json_store_dir(user_id, "eval_results")
        _save_json_file(store_dir / "_groups.json", data, user_id)


def load_eval_groups(user_id: str) -> Optional[dict[str, Any]]:
    """Load pre-computed groups response for a user."""
    if _s3_enabled():
        return _s3_load_json(user_id, "eval_results", "_groups.json")
    store_dir = _get_json_store_dir(user_id, "eval_results")
    return _load_json_file(store_dir / "_groups.json")


def save_eval_detail(user_id: str, group_id: str, data: dict[str, Any]) -> None:
    """Save pre-computed detail response for an eval group."""
    safe_id = group_id.replace("/", "_").replace("\\", "_")
    filename = f"detail_{safe_id}.json"
    if _s3_enabled():
        _s3_save_json(user_id, "eval_results", filename, data)
    else:
        store_dir = _get_json_store_dir(user_id, "eval_results")
        _save_json_file(store_dir / filename, data)


def load_eval_detail(user_id: str, group_id: str) -> Optional[dict[str, Any]]:
    """Load pre-computed detail response for an eval group."""
    safe_id = group_id.replace("/", "_").replace("\\", "_")
    filename = f"detail_{safe_id}.json"
    if _s3_enabled():
        return _s3_load_json(user_id, "eval_results", filename)
    store_dir = _get_json_store_dir(user_id, "eval_results")
    return _load_json_file(store_dir / filename)


# ============== Document Storage ==============

def get_user_documents_dir(user_id: str) -> Path:
    documents_dir = get_user_dir(user_id) / "documents"
    documents_dir.mkdir(parents=True, exist_ok=True)
    return documents_dir


def save_document(user_id: str, filename: str, content: bytes, folder: Optional[str] = None) -> Path:
    safe_filename = os.path.basename(filename)
    if not safe_filename:
        raise ValueError("Invalid filename: empty after sanitization")

    safe_folder = os.path.basename(folder) if folder else None

    if safe_folder:
        target_dir = get_user_documents_dir(user_id) / safe_folder
        target_dir.mkdir(parents=True, exist_ok=True)
    else:
        target_dir = get_user_documents_dir(user_id)

    filepath = target_dir / safe_filename

    if filepath.exists():
        stem = filepath.stem
        suffix = filepath.suffix
        counter = 2
        while filepath.exists():
            filepath = target_dir / f"{stem}_{counter}{suffix}"
            counter += 1

    filepath.write_bytes(content)
    return filepath


def list_user_documents(user_id: str) -> dict:
    documents_dir = get_user_documents_dir(user_id)

    result = {
        "files": [],
        "folders": {}
    }

    for item in documents_dir.iterdir():
        if item.is_file():
            result["files"].append(item.name)
        elif item.is_dir():
            result["folders"][item.name] = [f.name for f in item.iterdir() if f.is_file()]

    return result


# Media type mapping for documents
MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/plain",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".csv": "text/csv",
    ".py": "text/x-python",
}

MAX_DOCUMENTS = 100
MAX_DOCUMENT_SIZE_MB = 50


def get_document_content(user_id: str, doc_path: str) -> tuple[bytes, str]:
    """Load document content and detect media type."""
    ext = Path(doc_path).suffix.lower()
    if ext not in MEDIA_TYPES:
        raise ValueError(f"Unsupported file type: {ext}")

    media_type = MEDIA_TYPES[ext]

    if is_s3_enabled():
        content = get_document_content_from_s3(user_id, doc_path)
        size_mb = len(content) / (1024 * 1024)
        if size_mb > MAX_DOCUMENT_SIZE_MB:
            raise ValueError(f"Document '{doc_path}' is {size_mb:.1f}MB. Max is {MAX_DOCUMENT_SIZE_MB}MB.")
        return content, media_type

    documents_dir = get_user_documents_dir(user_id)
    filepath = (documents_dir / doc_path).resolve()

    if not filepath.is_relative_to(documents_dir.resolve()):
        raise ValueError(f"Invalid document path: {doc_path}")

    if not filepath.exists():
        raise FileNotFoundError(f"Document '{doc_path}' not found")

    size_mb = filepath.stat().st_size / (1024 * 1024)
    if size_mb > MAX_DOCUMENT_SIZE_MB:
        raise ValueError(f"Document '{doc_path}' is {size_mb:.1f}MB. Max is {MAX_DOCUMENT_SIZE_MB}MB.")

    content = filepath.read_bytes()
    return content, media_type


def list_user_document_paths(user_id: str) -> list[str]:
    """List all document paths for a user (flat list)."""
    if is_s3_enabled():
        docs = list_user_s3_documents(user_id)
        paths = []
        for doc in docs:
            rel_path = doc.get("path", "")
            if rel_path:
                if "/" in rel_path:
                    folder, timestamped_name = rel_path.rsplit("/", 1)
                    parts = timestamped_name.split("_", 2)
                    if len(parts) >= 3:
                        filename = parts[2]
                    else:
                        filename = timestamped_name
                    paths.append(f"{folder}/{filename}")
                else:
                    parts = rel_path.split("_", 2)
                    if len(parts) >= 3:
                        paths.append(parts[2])
                    else:
                        paths.append(rel_path)
        return paths

    documents_dir = get_user_documents_dir(user_id)
    paths = []

    if not documents_dir.exists():
        return paths

    for item in documents_dir.iterdir():
        if item.is_file():
            paths.append(item.name)
        elif item.is_dir():
            for f in item.iterdir():
                if f.is_file():
                    paths.append(f"{item.name}/{f.name}")

    return paths
