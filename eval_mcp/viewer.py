"""Local evaluation results viewer.

Serves the pre-built React comparison UI and the /api/compare/* endpoints.
Opens browser automatically.

Usage:
    eval-mcp view
    eval-mcp view --port 4001
"""

import os
import sys
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

STATIC_DIR = Path(__file__).parent / "viewer_static"


def create_viewer_app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/compare/groups")
    async def get_groups():
        from backend.core.eval_results import _read_log_headers, _build_groups_from_headers
        from backend.core.user_storage import get_user_log_dir

        user_id = os.environ.get("EVAL_MCP_USER", "local")
        log_dir = get_user_log_dir(user_id)

        headers = await _read_log_headers(log_dir)
        if not headers:
            return {"groups": []}
        return _build_groups_from_headers(headers)

    @app.get("/api/compare/detail")
    async def get_detail(group_id: str):
        from backend.core.eval_results import (
            _read_log_headers,
            _read_full_logs,
            _build_detail_from_logs,
        )
        from backend.core.user_storage import get_user_dir, get_user_log_dir

        user_id = os.environ.get("EVAL_MCP_USER", "local")
        log_dir = get_user_log_dir(user_id)
        user_dir = get_user_dir(user_id)

        headers = await _read_log_headers(log_dir)
        group_logs = [h for h in headers if (h.get("run_id") or h["file"]) == group_id]
        if not group_logs:
            raise HTTPException(status_code=404, detail="Group not found")

        log_files = [l["file"] for l in group_logs]
        full_logs = await _read_full_logs(log_files)
        if not full_logs:
            raise HTTPException(status_code=404, detail="No logs found")

        return _build_detail_from_logs(group_id, group_logs, full_logs, user_dir)

    @app.post("/api/compare/rebuild")
    async def rebuild():
        from backend.core.eval_results import precompute_eval_results
        user_id = os.environ.get("EVAL_MCP_USER", "local")
        await precompute_eval_results(user_id, force=True)
        return {"ok": True}

    # Auth stub for local viewer (only binds to 127.0.0.1, never deployed to AWS)
    @app.get("/api/auth/user")
    async def auth_user():
        return {"user": {}, "logoutUrl": "#"}

    # Serve static files
    if STATIC_DIR.exists():
        @app.get("/results")
        async def results_page():
            return FileResponse(STATIC_DIR / "results.html")

        @app.get("/")
        async def index():
            return FileResponse(STATIC_DIR / "results.html")

        app.mount("/_next", StaticFiles(directory=STATIC_DIR / "_next"), name="static")

    return app


def start_viewer(port: int = 4001):
    """Start the viewer server and open browser."""
    if "USER_STORAGE_BASE" not in os.environ:
        os.environ["USER_STORAGE_BASE"] = str(Path.home() / ".eval-mcp" / "users")

    app = create_viewer_app()

    print(f"Opening eval viewer at http://localhost:{port}/results")
    webbrowser.open(f"http://localhost:{port}/results")

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
