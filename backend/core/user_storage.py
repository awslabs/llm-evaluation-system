"""User storage helper for per-user file isolation.

Provides a simple interface for storing files in user-specific directories.
Local dev: backend/users/{user_id}/
EKS prod: /data/users/{user_id}/ (EBS volume, backed up periodically to S3)
AWS S3: When DOCUMENTS_BUCKET is set, documents are stored in S3.

SQLite storage: Judges, eval configs, and datasets are stored in the user's
promptfoo.db database (backed up periodically to S3).
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from backend.core.s3_client import (
    is_s3_enabled,
    get_document_content_from_s3,
    list_user_s3_documents,
)


def get_user_base_dir() -> Path:
    """Get the base directory for all user storage.

    Returns:
        Path to user storage base (e.g., backend/users or /data/users)
    """
    # Allow override via environment variable for EKS deployment
    base = os.environ.get("USER_STORAGE_BASE", "backend/users")
    return Path(base)


def get_user_dir(user_id: str) -> Path:
    """Get the directory for a specific user, creating it if needed.

    Args:
        user_id: The user's ID

    Returns:
        Path to user's directory (e.g., backend/users/user_123/)
    """
    if not user_id:
        raise ValueError("user_id is required")

    # Prevent path traversal attacks - user_id should be an email address
    # and must not contain path separators or parent directory references
    if '..' in user_id or '/' in user_id or '\\' in user_id:
        raise ValueError(f"Invalid user_id: contains path traversal characters")

    user_dir = get_user_base_dir() / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


def get_user_datasets_dir(user_id: str) -> Path:
    """Get the datasets directory for a user."""
    datasets_dir = get_user_dir(user_id) / "datasets"
    datasets_dir.mkdir(parents=True, exist_ok=True)
    return datasets_dir


def get_user_judges_dir(user_id: str) -> Path:
    """Get the judges directory for a user."""
    judges_dir = get_user_dir(user_id) / "judges"
    judges_dir.mkdir(parents=True, exist_ok=True)
    return judges_dir


def get_user_configs_dir(user_id: str) -> Path:
    """Get the configs directory for a user."""
    configs_dir = get_user_dir(user_id) / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    return configs_dir


def get_user_promptfoo_dir(user_id: str) -> Path:
    """Get the promptfoo config directory for a user.

    This is where promptfoo.db will be stored for this user.
    Set PROMPTFOO_CONFIG_DIR to this path before running evaluations.
    """
    return get_user_dir(user_id)


def save_dataset(user_id: str, filename: str, content: str) -> Path:
    """Save a dataset file for a user.

    Args:
        user_id: The user's ID
        filename: Name of the file (e.g., "healthcare_10.yaml")
        content: File content

    Returns:
        Path to saved file
    """
    # Sanitize filename to prevent path traversal
    safe_filename = os.path.basename(filename)
    if not safe_filename:
        raise ValueError("Invalid filename: empty after sanitization")
    filepath = get_user_datasets_dir(user_id) / safe_filename
    filepath.write_text(content)
    return filepath


def save_judge(user_id: str, filename: str, content: str) -> Path:
    """Save a judge file for a user.

    Args:
        user_id: The user's ID
        filename: Name of the file (e.g., "healthcare_judge.md")
        content: File content

    Returns:
        Path to saved file
    """
    # Sanitize filename to prevent path traversal
    safe_filename = os.path.basename(filename)
    if not safe_filename:
        raise ValueError("Invalid filename: empty after sanitization")
    filepath = get_user_judges_dir(user_id) / safe_filename
    filepath.write_text(content)
    return filepath


def save_config(user_id: str, filename: str, content: str) -> Path:
    """Save a config file for a user.

    Args:
        user_id: The user's ID
        filename: Name of the file (e.g., "evaluation.yaml")
        content: File content

    Returns:
        Path to saved file
    """
    # Sanitize filename to prevent path traversal
    safe_filename = os.path.basename(filename)
    if not safe_filename:
        raise ValueError("Invalid filename: empty after sanitization")
    filepath = get_user_configs_dir(user_id) / safe_filename
    filepath.write_text(content)
    return filepath


def list_user_files(user_id: str, folder: str, pattern: str = "*") -> list:
    """List files in a user's folder.

    Args:
        user_id: The user's ID
        folder: Folder name (datasets, judges, configs)
        pattern: Glob pattern (default: all files)

    Returns:
        List of Path objects
    """
    user_dir = get_user_dir(user_id)
    folder_path = user_dir / folder

    if not folder_path.exists():
        return []

    return list(folder_path.glob(pattern))


# ============== SQLite Storage ==============
# Judges, eval configs, and datasets are stored in the user's promptfoo.db
# This ensures they're backed up periodically to S3.


def get_user_db_path(user_id: str) -> Path:
    """Get the path to a user's promptfoo.db database."""
    return get_user_dir(user_id) / "promptfoo.db"


def _get_db_connection(user_id: str) -> sqlite3.Connection:
    """Get a connection to the user's promptfoo.db database."""
    db_path = get_user_db_path(user_id)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _generate_id(prefix: str) -> str:
    """Generate a unique ID with a prefix."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ============== Judges (stored in configs table with type='judge') ==============


def save_judge_to_db(user_id: str, name: str, config: dict[str, Any]) -> str:
    """Save a judge to the user's database.

    Args:
        user_id: The user's ID
        name: Name of the judge (e.g., "healthcare_criteria")
        config: Judge config dict with domain, criteria, etc.

    Returns:
        The judge ID
    """
    conn = _get_db_connection(user_id)
    try:
        judge_id = _generate_id("judge")
        now = int(datetime.now().timestamp() * 1000)

        conn.execute(
            """
            INSERT INTO configs (id, created_at, updated_at, name, type, config)
            VALUES (?, ?, ?, ?, 'judge', ?)
            """,
            (judge_id, now, now, name, json.dumps(config)),
        )
        conn.commit()
        return judge_id
    finally:
        conn.close()


def get_judge_from_db(user_id: str, judge_id: str) -> Optional[dict[str, Any]]:
    """Get a judge by ID from the user's database.

    Returns:
        Dict with id, name, config, created_at, or None if not found
    """
    conn = _get_db_connection(user_id)
    try:
        row = conn.execute(
            "SELECT id, name, config, created_at FROM configs WHERE id = ? AND type = 'judge'",
            (judge_id,),
        ).fetchone()

        if row:
            return {
                "id": row["id"],
                "name": row["name"],
                "config": json.loads(row["config"]),
                "created_at": row["created_at"],
            }
        return None
    finally:
        conn.close()


def get_judge_by_name(user_id: str, name: str) -> Optional[dict[str, Any]]:
    """Get a judge by name from the user's database.

    Returns:
        Dict with id, name, config, created_at, or None if not found
    """
    conn = _get_db_connection(user_id)
    try:
        row = conn.execute(
            "SELECT id, name, config, created_at FROM configs WHERE name = ? AND type = 'judge' ORDER BY created_at DESC LIMIT 1",
            (name,),
        ).fetchone()

        if row:
            return {
                "id": row["id"],
                "name": row["name"],
                "config": json.loads(row["config"]),
                "created_at": row["created_at"],
            }
        return None
    finally:
        conn.close()


def list_judges_from_db(user_id: str, search_term: str = "") -> list[dict[str, Any]]:
    """List all judges from the user's database.

    Args:
        user_id: The user's ID
        search_term: Optional search term to filter by name

    Returns:
        List of dicts with id, name, config, created_at
    """
    conn = _get_db_connection(user_id)
    try:
        if search_term:
            rows = conn.execute(
                "SELECT id, name, config, created_at FROM configs WHERE type = 'judge' AND name LIKE ? ORDER BY created_at DESC",
                (f"%{search_term}%",),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, name, config, created_at FROM configs WHERE type = 'judge' ORDER BY created_at DESC"
            ).fetchall()

        return [
            {
                "id": row["id"],
                "name": row["name"],
                "config": json.loads(row["config"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    finally:
        conn.close()


def delete_judge_from_db(user_id: str, judge_id: str) -> bool:
    """Delete a judge from the user's database.

    Returns:
        True if deleted, False if not found
    """
    conn = _get_db_connection(user_id)
    try:
        cursor = conn.execute(
            "DELETE FROM configs WHERE id = ? AND type = 'judge'",
            (judge_id,),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# ============== Eval Configs (stored in configs table with type='eval') ==============


def save_eval_config_to_db(user_id: str, name: str, config: dict[str, Any]) -> str:
    """Save an eval config to the user's database.

    Args:
        user_id: The user's ID
        name: Name of the config (e.g., "healthcare_eval")
        config: Eval config dict with providers, prompts, dataset_id, judge_id, etc.

    Returns:
        The config ID
    """
    conn = _get_db_connection(user_id)
    try:
        config_id = _generate_id("eval")
        now = int(datetime.now().timestamp() * 1000)

        conn.execute(
            """
            INSERT INTO configs (id, created_at, updated_at, name, type, config)
            VALUES (?, ?, ?, ?, 'eval', ?)
            """,
            (config_id, now, now, name, json.dumps(config)),
        )
        conn.commit()
        return config_id
    finally:
        conn.close()


def get_eval_config_from_db(user_id: str, config_id: str) -> Optional[dict[str, Any]]:
    """Get an eval config by ID from the user's database.

    Returns:
        Dict with id, name, config, created_at, or None if not found
    """
    conn = _get_db_connection(user_id)
    try:
        row = conn.execute(
            "SELECT id, name, config, created_at FROM configs WHERE id = ? AND type = 'eval'",
            (config_id,),
        ).fetchone()

        if row:
            return {
                "id": row["id"],
                "name": row["name"],
                "config": json.loads(row["config"]),
                "created_at": row["created_at"],
            }
        return None
    finally:
        conn.close()


def list_eval_configs_from_db(user_id: str, search_term: str = "") -> list[dict[str, Any]]:
    """List all eval configs from the user's database.

    Args:
        user_id: The user's ID
        search_term: Optional search term to filter by name

    Returns:
        List of dicts with id, name, config, created_at
    """
    conn = _get_db_connection(user_id)
    try:
        if search_term:
            rows = conn.execute(
                "SELECT id, name, config, created_at FROM configs WHERE type = 'eval' AND name LIKE ? ORDER BY created_at DESC",
                (f"%{search_term}%",),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, name, config, created_at FROM configs WHERE type = 'eval' ORDER BY created_at DESC"
            ).fetchall()

        return [
            {
                "id": row["id"],
                "name": row["name"],
                "config": json.loads(row["config"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    finally:
        conn.close()


# ============== Datasets (stored in datasets table) ==============


def save_dataset_to_db(user_id: str, name: str, tests: list[dict[str, Any]]) -> str:
    """Save a dataset to the user's database.

    Args:
        user_id: The user's ID
        name: Name of the dataset (e.g., "healthcare_10")
        tests: List of test cases in promptfoo format

    Returns:
        The dataset ID (hash of tests)
    """
    import hashlib

    conn = _get_db_connection(user_id)
    try:
        # Generate ID as hash of tests (sort_keys=True for consistent hashing)
        hash_json = json.dumps(tests, sort_keys=True)
        dataset_id = hashlib.sha256(hash_json.encode()).hexdigest()
        now = int(datetime.now().timestamp() * 1000)

        # Store with original key order (question before golden_answer)
        tests_json = json.dumps(tests)

        # Use INSERT OR REPLACE to handle duplicates
        conn.execute(
            """
            INSERT OR REPLACE INTO datasets (id, created_at, tests)
            VALUES (?, ?, ?)
            """,
            (dataset_id, now, tests_json),
        )

        # Also store the name mapping in configs table for easy lookup
        # Check if name mapping already exists
        existing = conn.execute(
            "SELECT id FROM configs WHERE name = ? AND type = 'dataset_name'",
            (name,),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE configs SET config = ?, updated_at = ? WHERE name = ? AND type = 'dataset_name'",
                (json.dumps({"dataset_id": dataset_id}), now, name),
            )
        else:
            conn.execute(
                """
                INSERT INTO configs (id, created_at, updated_at, name, type, config)
                VALUES (?, ?, ?, ?, 'dataset_name', ?)
                """,
                (_generate_id("dsname"), now, now, name, json.dumps({"dataset_id": dataset_id})),
            )

        conn.commit()
        return dataset_id
    finally:
        conn.close()


def get_dataset_from_db(user_id: str, dataset_id: str) -> Optional[dict[str, Any]]:
    """Get a dataset by ID from the user's database.

    Returns:
        Dict with id, tests, created_at, or None if not found
    """
    conn = _get_db_connection(user_id)
    try:
        row = conn.execute(
            "SELECT id, tests, created_at FROM datasets WHERE id = ?",
            (dataset_id,),
        ).fetchone()

        if row:
            return {
                "id": row["id"],
                "tests": json.loads(row["tests"]),
                "created_at": row["created_at"],
            }
        return None
    finally:
        conn.close()


def get_dataset_by_name(user_id: str, name: str) -> Optional[dict[str, Any]]:
    """Get a dataset by name from the user's database.

    Returns:
        Dict with id, name, tests, created_at, or None if not found
    """
    conn = _get_db_connection(user_id)
    try:
        # Look up dataset_id from name mapping
        name_row = conn.execute(
            "SELECT config FROM configs WHERE name = ? AND type = 'dataset_name'",
            (name,),
        ).fetchone()

        if not name_row:
            return None

        name_config = json.loads(name_row["config"])
        dataset_id = name_config.get("dataset_id")

        if not dataset_id:
            return None

        # Get the actual dataset
        row = conn.execute(
            "SELECT id, tests, created_at FROM datasets WHERE id = ?",
            (dataset_id,),
        ).fetchone()

        if row:
            return {
                "id": row["id"],
                "name": name,
                "tests": json.loads(row["tests"]),
                "created_at": row["created_at"],
            }
        return None
    finally:
        conn.close()


def list_datasets_from_db(user_id: str, search_term: str = "") -> list[dict[str, Any]]:
    """List all named datasets from the user's database.

    Args:
        user_id: The user's ID
        search_term: Optional search term to filter by name

    Returns:
        List of dicts with id, name, tests, created_at
    """
    conn = _get_db_connection(user_id)
    try:
        # Get all dataset name mappings
        if search_term:
            name_rows = conn.execute(
                "SELECT name, config FROM configs WHERE type = 'dataset_name' AND name LIKE ? ORDER BY created_at DESC",
                (f"%{search_term}%",),
            ).fetchall()
        else:
            name_rows = conn.execute(
                "SELECT name, config FROM configs WHERE type = 'dataset_name' ORDER BY created_at DESC"
            ).fetchall()

        results = []
        for name_row in name_rows:
            name = name_row["name"]
            name_config = json.loads(name_row["config"])
            dataset_id = name_config.get("dataset_id")

            if dataset_id:
                row = conn.execute(
                    "SELECT id, tests, created_at FROM datasets WHERE id = ?",
                    (dataset_id,),
                ).fetchone()

                if row:
                    tests = json.loads(row["tests"])
                    results.append({
                        "id": row["id"],
                        "name": name,
                        "tests": tests,
                        "num_samples": len(tests) if isinstance(tests, list) else 0,
                        "created_at": row["created_at"],
                    })

        return results
    finally:
        conn.close()


# ============== Document Storage ==============

def get_user_documents_dir(user_id: str) -> Path:
    """Get the documents directory for a user."""
    documents_dir = get_user_dir(user_id) / "documents"
    documents_dir.mkdir(parents=True, exist_ok=True)
    return documents_dir


def save_document(user_id: str, filename: str, content: bytes, folder: Optional[str] = None) -> Path:
    """Save a document file for a user.

    Args:
        user_id: The user's ID
        filename: Name of the file (e.g., "manual.pdf")
        content: File content as bytes
        folder: Optional subfolder name (e.g., "product_manual_20240107_1430")

    Returns:
        Path to saved file
    """
    # Sanitize filename - strip path components to prevent directory traversal
    # e.g., "../../etc/passwd" becomes "passwd"
    safe_filename = os.path.basename(filename)
    if not safe_filename:
        raise ValueError("Invalid filename: empty after sanitization")

    # Sanitize folder if provided (defense-in-depth, caller already sanitizes)
    safe_folder = os.path.basename(folder) if folder else None

    if safe_folder:
        target_dir = get_user_documents_dir(user_id) / safe_folder
        target_dir.mkdir(parents=True, exist_ok=True)
    else:
        target_dir = get_user_documents_dir(user_id)

    filepath = target_dir / safe_filename

    # Handle filename collision - append _2, _3, etc.
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
    """List all documents for a user.

    Returns:
        Dict with:
        - files: list of files in root documents/
        - folders: dict of folder_name -> list of files
    """
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
    ".py": "text/x-python",  # Python agent files for evaluation
}

# Limits for document processing
MAX_DOCUMENTS = 100
MAX_DOCUMENT_SIZE_MB = 50


def get_document_content(user_id: str, doc_path: str) -> tuple[bytes, str]:
    """Load document content and detect media type.

    Args:
        user_id: User ID
        doc_path: Path relative to documents/, e.g., "AT&T.pdf" or "my_folder/doc.pdf"

    Returns:
        (content_bytes, media_type)

    Raises:
        FileNotFoundError: If document doesn't exist
        ValueError: If file type not supported or exceeds size limit
    """
    # Check file extension first (works for both S3 and local)
    ext = Path(doc_path).suffix.lower()
    if ext not in MEDIA_TYPES:
        raise ValueError(f"Unsupported file type: {ext}")

    media_type = MEDIA_TYPES[ext]

    # Use S3 if enabled, otherwise local filesystem
    if is_s3_enabled():
        content = get_document_content_from_s3(user_id, doc_path)
        # Check size
        size_mb = len(content) / (1024 * 1024)
        if size_mb > MAX_DOCUMENT_SIZE_MB:
            raise ValueError(f"Document '{doc_path}' is {size_mb:.1f}MB. Max is {MAX_DOCUMENT_SIZE_MB}MB.")
        return content, media_type

    # Local filesystem
    documents_dir = get_user_documents_dir(user_id)
    filepath = (documents_dir / doc_path).resolve()

    # Prevent path traversal - ensure resolved path is within documents directory
    if not filepath.is_relative_to(documents_dir.resolve()):
        raise ValueError(f"Invalid document path: {doc_path}")

    if not filepath.exists():
        raise FileNotFoundError(f"Document '{doc_path}' not found")

    # Check file size
    size_mb = filepath.stat().st_size / (1024 * 1024)
    if size_mb > MAX_DOCUMENT_SIZE_MB:
        raise ValueError(f"Document '{doc_path}' is {size_mb:.1f}MB. Max is {MAX_DOCUMENT_SIZE_MB}MB.")

    content = filepath.read_bytes()

    return content, media_type


def list_user_document_paths(user_id: str) -> list[str]:
    """List all document paths for a user (flat list).

    Returns:
        List of paths like ["doc.pdf", "folder/other.txt"]
    """
    # Use S3 if enabled
    if is_s3_enabled():
        docs = list_user_s3_documents(user_id)
        # Extract just the filenames (strip timestamp prefix)
        paths = []
        for doc in docs:
            rel_path = doc.get("path", "")
            if rel_path:
                # Path format: {timestamp}_{filename} or {folder}/{timestamp}_{filename}
                # Extract the original filename by removing timestamp prefix
                if "/" in rel_path:
                    folder, timestamped_name = rel_path.rsplit("/", 1)
                    # Remove timestamp prefix (YYYYMMDD_HHMMSS_)
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

    # Local filesystem
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
