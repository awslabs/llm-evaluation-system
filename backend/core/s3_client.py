"""S3 client for document uploads via pre-signed URLs.

This module provides functionality for generating pre-signed URLs
that allow browsers to upload files directly to S3, bypassing
ALB/CloudFront body size limits.
"""

import os
import logging
from typing import Optional
from datetime import datetime

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)

# S3 bucket for document uploads (set via env var in AWS, empty for local)
DOCUMENTS_BUCKET = os.environ.get("S3_BUCKET", "") or os.environ.get("DOCUMENTS_BUCKET", "")

# Pre-signed URL expiration (1 hour)
PRESIGN_EXPIRATION = 3600

# AWS region
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")


def get_s3_client():
    """Get S3 client with appropriate config."""
    config = Config(
        region_name=AWS_REGION,
        signature_version="s3v4",
        s3={"addressing_style": "virtual"},
    )
    return boto3.client("s3", config=config)


def is_s3_enabled() -> bool:
    """Check if S3 upload is enabled (bucket configured)."""
    return bool(DOCUMENTS_BUCKET)


def generate_presigned_upload_url(
    user_id: str,
    filename: str,
    content_type: str,
    folder: Optional[str] = None,
) -> dict:
    """Generate a pre-signed URL for uploading a file to S3.

    Args:
        user_id: User ID for path isolation
        filename: Original filename
        content_type: MIME type of the file
        folder: Optional subfolder for grouping files

    Returns:
        Dict with:
        - upload_url: Pre-signed URL for PUT request
        - s3_key: The S3 object key where file will be stored
        - expires_in: Seconds until URL expires
    """
    if not DOCUMENTS_BUCKET:
        raise RuntimeError("DOCUMENTS_BUCKET not configured")

    # Build S3 key: users/{user_id}/documents/{folder?}/{timestamp}_{filename}
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_filename = "".join(c if c.isalnum() or c in ".-_" else "_" for c in filename)

    if folder:
        s3_key = f"users/{user_id}/documents/{folder}/{timestamp}_{safe_filename}"
    else:
        s3_key = f"users/{user_id}/documents/{timestamp}_{safe_filename}"

    s3_client = get_s3_client()

    # Generate pre-signed URL for PUT
    presigned_url = s3_client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": DOCUMENTS_BUCKET,
            "Key": s3_key,
            "ContentType": content_type,
        },
        ExpiresIn=PRESIGN_EXPIRATION,
    )

    logger.info(f"Generated presigned upload URL for user {user_id}: {s3_key}")

    return {
        "upload_url": presigned_url,
        "s3_key": s3_key,
        "bucket": DOCUMENTS_BUCKET,
        "expires_in": PRESIGN_EXPIRATION,
    }


def generate_presigned_download_url(s3_key: str) -> str:
    """Generate a pre-signed URL for downloading a file from S3.

    Args:
        s3_key: The S3 object key

    Returns:
        Pre-signed URL for GET request
    """
    if not DOCUMENTS_BUCKET:
        raise RuntimeError("DOCUMENTS_BUCKET not configured")

    s3_client = get_s3_client()

    presigned_url = s3_client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": DOCUMENTS_BUCKET,
            "Key": s3_key,
        },
        ExpiresIn=PRESIGN_EXPIRATION,
    )

    return presigned_url


def get_s3_document_content(s3_key: str) -> bytes:
    """Download document content from S3.

    Args:
        s3_key: The S3 object key

    Returns:
        File content as bytes
    """
    if not DOCUMENTS_BUCKET:
        raise RuntimeError("DOCUMENTS_BUCKET not configured")

    s3_client = get_s3_client()

    response = s3_client.get_object(Bucket=DOCUMENTS_BUCKET, Key=s3_key)
    return response["Body"].read()


def get_document_content_from_s3(user_id: str, doc_path: str) -> bytes:
    """Get document content by finding matching file in S3.

    The doc_path from the frontend might be just the filename (e.g., "1706.03762v7.pdf")
    but S3 keys have timestamps (e.g., "20260108_161337_1706.03762v7.pdf").

    Args:
        user_id: User ID
        doc_path: Document path/filename

    Returns:
        File content as bytes

    Raises:
        FileNotFoundError: If document not found in S3
    """
    if not DOCUMENTS_BUCKET:
        raise RuntimeError("DOCUMENTS_BUCKET not configured")

    s3_client = get_s3_client()
    prefix = f"users/{user_id}/documents/"

    # List objects to find matching file
    paginator = s3_client.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=DOCUMENTS_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            # Extract filename from key (remove prefix and timestamp)
            rel_path = key[len(prefix):]

            # Check if this matches the requested doc_path
            # Key format: {timestamp}_{filename} or {folder}/{timestamp}_{filename}
            # doc_path could be: filename, or folder/filename

            # Handle folder case
            if "/" in doc_path:
                folder, filename = doc_path.rsplit("/", 1)
                if rel_path.startswith(f"{folder}/") and rel_path.endswith(filename):
                    return get_s3_document_content(key)
            else:
                # No folder - match just the filename part
                if rel_path.endswith(doc_path) or rel_path.endswith(f"_{doc_path}"):
                    return get_s3_document_content(key)

    raise FileNotFoundError(f"Document '{doc_path}' not found in S3")


def list_user_s3_documents(user_id: str) -> list[dict]:
    """List all documents for a user in S3.

    Args:
        user_id: User ID

    Returns:
        List of dicts with key, size, last_modified
    """
    if not DOCUMENTS_BUCKET:
        return []

    s3_client = get_s3_client()
    prefix = f"users/{user_id}/documents/"

    documents = []
    paginator = s3_client.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=DOCUMENTS_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            # Extract relative path from key
            rel_path = obj["Key"][len(prefix):]
            if rel_path:  # Skip the prefix itself
                documents.append({
                    "key": obj["Key"],
                    "path": rel_path,
                    "size": obj["Size"],
                    "last_modified": obj["LastModified"].isoformat(),
                })

    return documents
