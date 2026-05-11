"""S3 sync for eval-mcp.

Storage model:
  Local disk is the source of truth. S3 is a continuous mirror used for
  durability and team sharing. When `bucket` is configured:
    - Every write triggers an async background upload (replicate_async).
    - Every read does a quick pull-down first (auto_pull, debounced).
    - On S3 failure, log a warning and fall back to local; the next sync
      reconciles.
  When no bucket is configured, all helpers no-op.

Layout in S3:
  s3://{bucket}/users/{user}/...    ← personal mirror of ~/.eval-mcp/users/{user}/
  s3://{bucket}/projects/{name}/... ← shared evals (promoted via `eval-mcp share`)
"""

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional, Set

import boto3
from botocore.config import Config

from eval_mcp.config import get_bucket, get_sync_region, get_user, get_projects
from eval_mcp.storage import get_logs_dir

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Tree walking — what counts as a syncable file
# ----------------------------------------------------------------------------

_SKIP_PARTS = {"__pycache__", "temp", "store/eval_results"}
_SKIP_NAMES = {".DS_Store"}


def _user_root(user_id: Optional[str] = None) -> Path:
    """Local user dir: ~/.eval-mcp/users/{user}/. Mirrors the deployed layout."""
    base = Path(os.environ.get("USER_STORAGE_BASE", Path.home() / ".eval-mcp" / "users"))
    return base / (user_id or get_user())


def _is_syncable(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    root_resolved = root.resolve()
    if not resolved.is_relative_to(root_resolved):
        return False
    if not resolved.is_file():
        return False
    if resolved.name in _SKIP_NAMES or resolved.name.endswith(".pyc"):
        return False
    rel = resolved.relative_to(root_resolved)
    return not any(part in _SKIP_PARTS for part in rel.parts)


def _iter_local_files(root: Path):
    if not root.exists():
        return
    for path in root.rglob("*"):
        if _is_syncable(path, root):
            yield path


def _key_for(path: Path, root: Path, prefix: str) -> str:
    rel = path.relative_to(root).as_posix()
    return f"{prefix}{rel}"


# ----------------------------------------------------------------------------
# S3 client + low-level helpers
# ----------------------------------------------------------------------------

def _get_s3_client():
    region = get_sync_region() or os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    config = Config(retries={"max_attempts": 3})
    return boto3.client("s3", region_name=region, config=config)


def _list_remote_keys(s3, bucket: str, prefix: str) -> Set[str]:
    keys: Set[str] = set()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.add(obj["Key"])
    return keys


def _discover_projects(s3, bucket: str) -> List[str]:
    projects = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="projects/", Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            name = cp["Prefix"].removeprefix("projects/").rstrip("/")
            if name:
                projects.append(name)
    return projects


# ----------------------------------------------------------------------------
# Async replication (write-through)
# ----------------------------------------------------------------------------

_replicate_pool: Optional[ThreadPoolExecutor] = None
_pool_lock = threading.Lock()


def _get_pool() -> ThreadPoolExecutor:
    global _replicate_pool
    with _pool_lock:
        if _replicate_pool is None:
            _replicate_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="eval-mcp-replicate")
        return _replicate_pool


def replicate_async(local_path: Path, user_id: Optional[str] = None) -> None:
    """Fire-and-forget upload of `local_path` to its mirrored S3 location.

    Path is mirrored under `users/{user}/` preserving the directory layout
    relative to the user's local root. No-op if bucket isn't configured.
    """
    bucket = get_bucket()
    if not bucket:
        return

    try:
        root = _user_root(user_id).resolve()
        local_path = Path(local_path).resolve()
        # Path must live under the user root for the key to mirror correctly.
        if not local_path.is_relative_to(root):
            logger.debug("skipping replicate; path outside user root: %s", local_path)
            return
        if not local_path.exists() or not local_path.is_file():
            return
        if not _is_syncable(local_path, root):
            return
    except Exception as e:
        logger.warning("replicate_async preflight failed for %s: %s", local_path, e)
        return

    user = user_id or get_user()
    prefix = f"users/{user}/"
    key = _key_for(local_path, root, prefix)

    def _do_upload():
        try:
            _get_s3_client().upload_file(str(local_path), bucket, key)
            logger.debug("replicated %s -> s3://%s/%s", local_path.name, bucket, key)
        except Exception as e:
            logger.warning("replicate failed for %s: %s", local_path, e)

    _get_pool().submit(_do_upload)


# ----------------------------------------------------------------------------
# Auto-pull (read-side, debounced)
# ----------------------------------------------------------------------------

_LAST_PULL: dict = {}
_PULL_TTL_SEC = 5.0
_pull_lock = threading.Lock()


def auto_pull(user_id: Optional[str] = None, ttl: float = _PULL_TTL_SEC) -> None:
    """Pull missing files from S3 into the local user tree.

    Debounced: repeated calls within `ttl` seconds are coalesced to a single
    pull. Safe to call before every list/read tool — no-op if bucket isn't
    configured, log-and-continue on S3 failure.
    """
    bucket = get_bucket()
    if not bucket:
        return

    user = user_id or get_user()
    now = time.monotonic()
    with _pull_lock:
        last = _LAST_PULL.get(user, 0.0)
        if now - last < ttl:
            return
        _LAST_PULL[user] = now

    try:
        sync_down(user_id=user)
    except Exception as e:
        logger.warning("auto_pull failed: %s", e)


# ----------------------------------------------------------------------------
# Full sync (manual + recovery)
# ----------------------------------------------------------------------------

def sync_up(user_id: Optional[str] = None) -> dict:
    """Upload every local file under the user root that is missing in S3."""
    bucket = get_bucket()
    if not bucket:
        return {"synced": 0, "skipped": True}

    user = user_id or get_user()
    root = _user_root(user)
    root.mkdir(parents=True, exist_ok=True)

    prefix = f"users/{user}/"
    s3 = _get_s3_client()
    remote_keys = _list_remote_keys(s3, bucket, prefix)

    uploaded = 0
    for path in _iter_local_files(root):
        key = _key_for(path, root, prefix)
        if key in remote_keys:
            continue
        try:
            s3.upload_file(str(path), bucket, key)
            uploaded += 1
        except Exception as e:
            logger.warning("sync_up failed for %s: %s", path, e)

    return {"synced": uploaded}


def sync_down(user_id: Optional[str] = None) -> dict:
    """Download every remote file under user prefix + project prefixes that
    is missing locally.
    """
    bucket = get_bucket()
    if not bucket:
        return {"synced": 0, "skipped": True}

    user = user_id or get_user()
    root = _user_root(user)
    root.mkdir(parents=True, exist_ok=True)

    s3 = _get_s3_client()

    prefixes = [f"users/{user}/"]
    projects = get_projects() or _discover_projects(s3, bucket)
    for project in projects:
        prefixes.append(f"projects/{project}/")

    downloaded = 0
    for prefix in prefixes:
        remote_keys = _list_remote_keys(s3, bucket, prefix)
        for key in remote_keys:
            rel = key[len(prefix):]
            if not rel:
                continue
            local_path = root / rel
            if local_path.exists():
                continue
            try:
                local_path.parent.mkdir(parents=True, exist_ok=True)
                s3.download_file(bucket, key, str(local_path))
                downloaded += 1
            except Exception as e:
                logger.warning("sync_down failed for %s: %s", key, e)

    return {"synced": downloaded, "projects": projects}


def sync_to_project(project: str, user_id: Optional[str] = None) -> dict:
    """Promote (upload) every local file under user root to the shared
    `projects/{project}/` prefix. Used by `eval-mcp share`.
    """
    bucket = get_bucket()
    if not bucket:
        return {"synced": 0, "skipped": True}

    root = _user_root(user_id)
    prefix = f"projects/{project}/"
    s3 = _get_s3_client()
    remote_keys = _list_remote_keys(s3, bucket, prefix)

    uploaded = 0
    for path in _iter_local_files(root):
        key = _key_for(path, root, prefix)
        if key in remote_keys:
            continue
        try:
            s3.upload_file(str(path), bucket, key)
            uploaded += 1
        except Exception as e:
            logger.warning("share failed for %s: %s", path, e)

    return {"synced": uploaded, "project": project}


# ----------------------------------------------------------------------------
# Backwards-compatible aliases (used by CLI today)
# ----------------------------------------------------------------------------

def sync_logs_up(user_id: Optional[str] = None, log_dir: Optional[Path] = None) -> dict:
    return sync_up(user_id=user_id)


def sync_logs_down(user_id: Optional[str] = None, log_dir: Optional[Path] = None) -> dict:
    return sync_down(user_id=user_id)


def sync_logs_to_project(project: str, user_id: Optional[str] = None, log_dir: Optional[Path] = None) -> dict:
    return sync_to_project(project, user_id=user_id)
