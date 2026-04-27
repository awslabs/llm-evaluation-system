"""Thin adapter layer for Inspect AI viewer integration.

This is the ONLY file that imports from inspect_ai._view internals.
When upgrading Inspect AI, check this file first for compatibility.
"""

from typing import Any

from fastapi import FastAPI, Request
from starlette.staticfiles import StaticFiles

from inspect_ai._util.file import filesystem
from inspect_ai._view.fastapi_server import (
    AccessPolicy,
    FileMappingPolicy,
    _InspectStaticFiles,
    view_server_app,
)
from inspect_ai._view._dist import resolve_dist_directory


class UserAccessPolicy:
    """Multi-tenant access policy. Scopes log access to the authenticated user."""

    async def can_read(self, request: Request, file: str) -> bool:
        user_id = _get_user_id(request)
        if not user_id:
            return False
        return f"/{user_id}/" in file or file.startswith(f"{user_id}/")

    async def can_delete(self, request: Request, file: str) -> bool:
        return await self.can_read(request, file)

    async def can_list(self, request: Request, dir: str) -> bool:
        return True


class UserFileMappingPolicy:
    """Maps file paths to per-user directories within the log root."""

    def __init__(self, log_root: str):
        self._log_root = log_root.rstrip("/")

    async def map(self, request: Request, file: str) -> str:
        user_id = _get_user_id(request)
        if not user_id:
            return file
        if file.startswith(self._log_root):
            return file
        return f"{self._log_root}/{user_id}/logs/{file}"

    async def unmap(self, request: Request, file: str) -> str:
        user_id = _get_user_id(request)
        if not user_id:
            return file
        prefix = f"{self._log_root}/{user_id}/logs/"
        if file.startswith(prefix):
            return file[len(prefix):]
        return file


def _get_user_id(request: Request) -> str | None:
    """Extract user ID from request headers."""
    return request.headers.get("X-Forwarded-User") or request.headers.get("x-user-id")


def create_viewer_app(
    log_dir: str,
    fs_options: dict[str, Any] | None = None,
    multi_tenant: bool = False,
) -> FastAPI:
    """Create an Inspect viewer FastAPI app.

    Args:
        log_dir: Root directory for eval logs (local path or s3:// URL).
        fs_options: Options for filesystem access (e.g., S3 credentials).
        multi_tenant: If True, enable per-user access policies.
    """
    access_policy = UserAccessPolicy() if multi_tenant else None
    mapping_policy = UserFileMappingPolicy(log_dir) if multi_tenant else None

    # Resolve log_dir to full path (same as view_server() does)
    fs = filesystem(log_dir)
    if not fs.exists(log_dir):
        fs.mkdir(log_dir, True)
    resolved_dir = fs.info(log_dir).name

    api = view_server_app(
        default_dir=resolved_dir,
        access_policy=access_policy,
        mapping_policy=mapping_policy,
        fs_options=fs_options or {},
    )

    dist_dir = resolve_dist_directory()

    @api.get("/dist")
    async def api_dist() -> dict[str, str]:
        return {"path": dist_dir.as_posix()}

    return api


def create_full_viewer(
    log_dir: str,
    fs_options: dict[str, Any] | None = None,
    multi_tenant: bool = False,
) -> FastAPI:
    """Create a complete Inspect viewer app with API + SPA.

    Mirrors Inspect's own view_server() assembly: API at /api, SPA at /.
    """
    api = create_viewer_app(log_dir, fs_options, multi_tenant)
    dist_dir = resolve_dist_directory()

    app = FastAPI()
    app.mount("/api", api)
    app.mount(
        "/",
        _InspectStaticFiles(directory=dist_dir.as_posix(), html=True),
        name="static",
    )
    return app


def get_viewer_dist_directory() -> str:
    """Get the path to Inspect's React SPA dist directory."""
    return resolve_dist_directory().as_posix()
