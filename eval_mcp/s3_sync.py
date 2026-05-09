"""S3 sync for eval logs.

Supports multi-tenant structure:
  s3://{bucket}/
    projects/{project}/    ← shared team evals
    users/{user}/          ← personal evals

Upload goes to users/{user}/.
Download pulls from users/{user}/ + all projects in the bucket.
"""

import logging
import os
from pathlib import Path
from typing import List, Optional

import boto3
from botocore.config import Config

from eval_mcp.config import get_bucket, get_sync_region, get_user, get_projects
from eval_mcp.storage import get_logs_dir

logger = logging.getLogger(__name__)


def _get_s3_client():
    region = get_sync_region() or os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    config = Config(retries={"max_attempts": 3})
    return boto3.client("s3", region_name=region, config=config)


def _list_remote_files(s3, bucket: str, prefix: str) -> set:
    remote = set()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            filename = key[len(prefix):]
            if filename and "/" not in filename:
                remote.add(filename)
    return remote


def _discover_projects(s3, bucket: str) -> List[str]:
    """List all project prefixes in the bucket."""
    projects = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="projects/", Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            # "projects/alpha/" → "alpha"
            name = cp["Prefix"].removeprefix("projects/").rstrip("/")
            if name:
                projects.append(name)
    return projects


def sync_logs_up(user_id: Optional[str] = None, log_dir: Optional[Path] = None) -> dict:
    """Upload new local .eval logs to the user's S3 prefix."""
    bucket = get_bucket()
    if not bucket:
        return {"synced": 0, "skipped": True}

    if user_id is None:
        user_id = get_user()
    if log_dir is None:
        log_dir = get_logs_dir()

    prefix = f"users/{user_id}/"
    local_files = {f.name for f in log_dir.iterdir() if f.name.endswith(".eval")}
    if not local_files:
        return {"synced": 0, "total_local": 0}

    s3 = _get_s3_client()
    remote_files = _list_remote_files(s3, bucket, prefix)
    to_upload = local_files - remote_files

    for filename in to_upload:
        filepath = log_dir / filename
        s3.upload_file(str(filepath), bucket, f"{prefix}{filename}")
        logger.info(f"Uploaded {filename} to s3://{bucket}/{prefix}{filename}")

    return {"synced": len(to_upload), "total_local": len(local_files)}


def sync_logs_down(user_id: Optional[str] = None, log_dir: Optional[Path] = None) -> dict:
    """Download missing .eval logs from user prefix + all accessible projects."""
    bucket = get_bucket()
    if not bucket:
        return {"synced": 0, "skipped": True}

    if user_id is None:
        user_id = get_user()
    if log_dir is None:
        log_dir = get_logs_dir()

    log_dir.mkdir(parents=True, exist_ok=True)
    local_files = {f.name for f in log_dir.iterdir() if f.name.endswith(".eval")}

    s3 = _get_s3_client()

    # User's own logs
    prefixes = [f"users/{user_id}/"]

    # Configured projects, or auto-discover all
    projects = get_projects()
    if not projects:
        projects = _discover_projects(s3, bucket)

    for project in projects:
        prefixes.append(f"projects/{project}/")

    total_synced = 0
    for prefix in prefixes:
        remote_files = _list_remote_files(s3, bucket, prefix)
        to_download = remote_files - local_files

        for filename in to_download:
            filepath = log_dir / filename
            s3.download_file(bucket, f"{prefix}{filename}", str(filepath))
            logger.info(f"Downloaded {filename} from s3://{bucket}/{prefix}{filename}")

        total_synced += len(to_download)
        local_files.update(to_download)

    return {"synced": total_synced, "projects": projects}


def sync_logs_to_project(project: str, user_id: Optional[str] = None, log_dir: Optional[Path] = None) -> dict:
    """Upload logs to a shared project prefix (for sharing with team)."""
    bucket = get_bucket()
    if not bucket:
        return {"synced": 0, "skipped": True}

    if user_id is None:
        user_id = get_user()
    if log_dir is None:
        log_dir = get_logs_dir()

    prefix = f"projects/{project}/"
    local_files = {f.name for f in log_dir.iterdir() if f.name.endswith(".eval")}
    if not local_files:
        return {"synced": 0, "total_local": 0}

    s3 = _get_s3_client()
    remote_files = _list_remote_files(s3, bucket, prefix)
    to_upload = local_files - remote_files

    for filename in to_upload:
        filepath = log_dir / filename
        s3.upload_file(str(filepath), bucket, f"{prefix}{filename}")
        logger.info(f"Shared {filename} to s3://{bucket}/{prefix}{filename}")

    return {"synced": len(to_upload), "project": project}
