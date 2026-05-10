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
        from eval_mcp.core.eval_results import _read_log_headers, _build_groups_from_headers
        from eval_mcp.core.user_storage import get_user_log_dir

        user_id = os.environ.get("EVAL_MCP_USER", "local")
        log_dir = get_user_log_dir(user_id)

        headers = await _read_log_headers(log_dir)
        if not headers:
            return {"groups": []}
        return _build_groups_from_headers(headers)

    @app.get("/api/compare/detail")
    async def get_detail(group_id: str):
        from eval_mcp.core.eval_results import (
            _read_log_headers,
            _read_full_logs,
            _build_detail_from_logs,
        )
        from eval_mcp.core.user_storage import get_user_dir, get_user_log_dir

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
        from eval_mcp.core.eval_results import precompute_eval_results
        user_id = os.environ.get("EVAL_MCP_USER", "local")
        await precompute_eval_results(user_id, force=True)
        return {"ok": True}

    @app.get("/api/compare/report/{group_id}")
    async def download_report(group_id: str):
        """Serve pre-generated PDF report for a group."""
        from eval_mcp.core.user_storage import get_user_dir

        user_id = os.environ.get("EVAL_MCP_USER", "local")
        safe_id = group_id.replace("/", "_").replace("\\", "_")
        pdf_path = get_user_dir(user_id) / "reports" / f"report_{safe_id}.pdf"

        if not pdf_path.exists():
            raise HTTPException(
                status_code=404,
                detail="Report not generated yet. Ask the agent to call generate_report.",
            )

        return FileResponse(
            path=str(pdf_path),
            media_type="application/pdf",
            filename=f"eval_report_{safe_id}.pdf",
        )

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
    """Start the viewer server and open browser (blocking, for `eval-mcp view`)."""
    if "USER_STORAGE_BASE" not in os.environ:
        os.environ["USER_STORAGE_BASE"] = str(Path.home() / ".eval-mcp" / "users")

    app = create_viewer_app()

    print(f"Opening eval viewer at http://localhost:{port}/results")
    webbrowser.open(f"http://localhost:{port}/results")

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def _is_viewer_running(port: int) -> bool:
    """Cheap TCP probe: does something already listen on localhost:{port}?"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        try:
            return s.connect_ex(("127.0.0.1", port)) == 0
        except OSError:
            return False


def ensure_viewer_running(port: int = 4001, open_path: str = "/results") -> dict:
    """Start the viewer in the background if not already running, then open the browser.

    Returns:
        dict with:
            url: the URL attempted
            started: True if we spawned a new viewer process this call
            alreadyRunning: True if the port was already bound
            browserOpened: True if we opened the browser (only on verified-running viewer)
            error: str if something went wrong
    """
    import subprocess
    import sys as _sys
    import time

    url = f"http://localhost:{port}{open_path}"

    if _is_viewer_running(port):
        webbrowser.open(url)
        return {"url": url, "started": False, "alreadyRunning": True, "browserOpened": True}

    # Spawn a detached viewer. `python -m eval_mcp view …` uses the same
    # interpreter the MCP is running in — no PATH lookup, no venv guessing.
    # stderr is captured to a file so we can diagnose crashes; stdout is
    # discarded (uvicorn's startup banner isn't useful here).
    log_path = Path(os.environ.get("TMPDIR", "/tmp")) / "eval-mcp-viewer.log"
    try:
        with open(log_path, "ab") as log_file:
            proc = subprocess.Popen(
                [_sys.executable, "-m", "eval_mcp", "view", "--port", str(port)],
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )
    except Exception as e:
        return {"url": url, "started": False, "alreadyRunning": False,
                "browserOpened": False, "error": f"spawn failed: {e}"}

    # Poll for the port. If the child dies before binding (e.g. port conflict,
    # import error) stop waiting and surface the failure — don't open a browser
    # tab that will just show "refused to connect".
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if _is_viewer_running(port):
            webbrowser.open(url)
            return {"url": url, "started": True, "alreadyRunning": False, "browserOpened": True}
        if proc.poll() is not None:
            return {
                "url": url, "started": False, "alreadyRunning": False, "browserOpened": False,
                "error": f"viewer exited with code {proc.returncode}; see {log_path}",
            }
        time.sleep(0.1)

    # Timed out. Leave the child alive in case it binds slightly later, but
    # don't lie about success.
    return {
        "url": url, "started": True, "alreadyRunning": False, "browserOpened": False,
        "error": f"viewer did not bind port {port} within 5s; check {log_path}",
    }
