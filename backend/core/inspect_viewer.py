"""Thin adapter layer for Inspect AI viewer integration.

This is the ONLY file that imports from inspect_ai._view internals.
When upgrading Inspect AI, check this file first for compatibility.
"""

import posixpath
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


def _normalize_key(path: str) -> str:
    """Reduce any log path to a comparable, traversal-collapsed key.

    Strips a `file://`/`s3://`/`gs://`-style scheme down to its path/key, then
    collapses `..`/`.` segments with posixpath.normpath. This MUST run before
    any boundary comparison — otherwise `/a/b/../../victim/x` or a scheme
    prefix would slip past a naive startswith/substring check.
    """
    if not path:
        return ""
    # Strip scheme: "file:///data/users/u/x" -> "/data/users/u/x";
    # "s3://bucket/users/u/x" -> "bucket/users/u/x". Both sides of every
    # comparison are normalized the same way, so the s3 leading-slash
    # difference is consistent and safe.
    if "://" in path:
        path = path.split("://", 1)[1]
    return posixpath.normpath(path)


def _is_within_dir(path: str, scope_dir: str) -> bool:
    """True iff normalized `path` is `scope_dir` itself or strictly under it.

    The boundary primitive: both sides are normalized (scheme stripped, `..`
    collapsed) and the match is on a path-SEPARATOR boundary, so neither a bare
    substring (`/users/x/` appearing mid-path) nor a sibling prefix
    (`{scope}-evil`) nor a `..` traversal can pass. Empty inputs are denied.
    """
    norm = _normalize_key(path)
    scope = _normalize_key(scope_dir).rstrip("/")
    if not norm or not scope:
        return False
    return norm == scope or (norm + "/").startswith(scope + "/")


def _is_within_user_scope(path: str, user_id: str, log_root: str) -> bool:
    """True iff `path` is inside the caller's per-user subtree under log_root.

    The single chokepoint for the Inspect viewer, where `log_root` is the
    shared base (e.g. `/data/users`) and each tenant owns `{log_root}/{user_id}`.
    Missing/empty user_id is always denied.
    """
    if not user_id:
        return False
    root = _normalize_key(log_root).rstrip("/")
    return _is_within_dir(path, f"{root}/{user_id}")


class UserAccessPolicy:
    """Multi-tenant access policy. Scopes log access to the authenticated user."""

    def __init__(self, log_root: str):
        # Normalize once (strip scheme, collapse) so the bare equality check in
        # can_list compares like-for-like with normalized request dirs.
        self._log_root = _normalize_key(log_root).rstrip("/")

    async def can_read(self, request: Request, file: str) -> bool:
        return _is_within_user_scope(file, _get_user_id(request), self._log_root)

    async def can_delete(self, request: Request, file: str) -> bool:
        return await self.can_read(request, file)

    async def can_write(self, request: Request, file: str) -> bool:
        return await self.can_read(request, file)

    async def can_list(self, request: Request, dir: str) -> bool:
        user_id = _get_user_id(request)
        if not user_id:
            return False
        # Allow the user's own subtree, OR the shared root itself — the root is
        # the viewer's default listing target, and the mapping policy rewrites
        # it down to THIS user's logs dir (see map()), so listing the root can
        # never enumerate another tenant. Any OTHER explicit dir (another
        # user's subtree, /tmp, …) is denied here.
        if _is_within_user_scope(dir, user_id, self._log_root):
            return True
        return _normalize_key(dir) == self._log_root


class UserFileMappingPolicy:
    """Maps file paths to per-user directories within the log root."""

    def __init__(self, log_root: str):
        self._log_root = _normalize_key(log_root).rstrip("/")

    async def map(self, request: Request, file: str) -> str:
        user_id = _get_user_id(request)
        if not user_id:
            return file
        user_logs = f"{self._log_root}/{user_id}/logs"
        # Already inside this user's subtree → pass through unchanged.
        if _is_within_user_scope(file, user_id, self._log_root):
            return file
        # The shared root (the default listing target) or any other absolute
        # path under the root that is NOT this user's → force into the user's
        # own logs dir. A caller-supplied path can never escape the per-user
        # prefix this way.
        if _is_within_dir(file, self._log_root):
            return user_logs
        # A relative filename → resolve under the user's logs dir.
        return f"{user_logs}/{file}"

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
    # Resolve log_dir to full path (same as view_server() does)
    fs = filesystem(log_dir)
    if not fs.exists(log_dir):
        fs.mkdir(log_dir, True)
    resolved_dir = fs.info(log_dir).name

    # Scope policies against the RESOLVED root so boundary checks compare
    # like-for-like with the absolute paths the viewer hands them.
    access_policy = UserAccessPolicy(resolved_dir) if multi_tenant else None
    mapping_policy = UserFileMappingPolicy(resolved_dir) if multi_tenant else None

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
