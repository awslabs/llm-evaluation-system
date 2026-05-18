"""Local evaluation results viewer.

Serves the pre-built React comparison UI and the /api/compare/* endpoints.
Opens browser automatically.

Usage:
    eval-mcp view
    eval-mcp view --port 4001
"""

import csv
import io
import json
import os
import sys
import webbrowser
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.responses import FileResponse, StreamingResponse

STATIC_DIR = Path(__file__).parent / "viewer_static"


def create_viewer_app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/compare/groups")
    async def get_groups():
        from eval_mcp.core.eval_results import precompute_eval_results
        from eval_mcp.core.user_storage import load_eval_groups

        user_id = os.environ.get("EVAL_MCP_USER", "local")
        cached = load_eval_groups(user_id)
        if cached:
            return cached
        await precompute_eval_results(user_id)
        return load_eval_groups(user_id) or {"groups": []}

    @app.get("/api/compare/detail")
    async def get_detail(group_id: str):
        from eval_mcp.core.eval_results import precompute_eval_results
        from eval_mcp.core.user_storage import load_eval_detail

        user_id = os.environ.get("EVAL_MCP_USER", "local")
        data = load_eval_detail(user_id, group_id)
        if data:
            return data
        await precompute_eval_results(user_id)
        data = load_eval_detail(user_id, group_id)
        if data:
            return data
        raise HTTPException(status_code=404, detail="Group not found")

    @app.post("/api/compare/rebuild")
    async def rebuild():
        from eval_mcp.core.eval_results import precompute_eval_results
        user_id = os.environ.get("EVAL_MCP_USER", "local")
        await precompute_eval_results(user_id, force=True)
        return {"ok": True}

    @app.get("/api/compare/report/{group_id}")
    async def download_report(group_id: str):
        """Serve pre-generated PDF report for a group."""
        from eval_mcp.core.user_storage import get_user_base_dir

        user_id = os.environ.get("EVAL_MCP_USER", "local")
        if not user_id or '/' in user_id or '\\' in user_id or user_id in ('.', '..'):
            raise HTTPException(status_code=400, detail="invalid user_id")
        safe_id = group_id.replace("/", "_").replace("\\", "_")
        filename = f"report_{safe_id}.pdf"

        base_real = os.path.realpath(str(get_user_base_dir()))
        pdf_real = os.path.realpath(os.path.join(base_real, user_id, "reports", filename))
        if not pdf_real.startswith(base_real + os.sep):
            raise HTTPException(status_code=400, detail="invalid path")

        if not os.path.isfile(pdf_real):
            raise HTTPException(
                status_code=404,
                detail="Report not generated yet. Ask the agent to call generate_report.",
            )

        return FileResponse(
            path=pdf_real,
            media_type="application/pdf",
            filename=f"eval_report_{safe_id}.pdf",
        )

    # Auth stub for local viewer (only binds to 127.0.0.1, never deployed to AWS)
    @app.get("/api/auth/user")
    async def auth_user():
        # `mode: "viewer"` tells the frontend it's running against the local
        # eval-mcp viewer (no chat backend, no Postgres). The Header uses
        # this to hide nav entries that would otherwise 404.
        return {
            "user": {"id": "local", "name": "local", "email": "local@dev"},
            "logoutUrl": "#",
            "mode": "viewer",
        }

    # ---------- Data Library (datasets + judges) ----------
    #
    # Mirrors backend/api/main.py but pulls user_id from EVAL_MCP_USER
    # instead of the oauth2-proxy header. Same storage layer, same
    # response shape, so the same frontend works in either deployment.

    def _viewer_user() -> str:
        return os.environ.get("EVAL_MCP_USER", "local")

    class _DatasetPatch(BaseModel):
        name: Optional[str] = None
        tests: Optional[list[dict]] = None

    @app.get("/api/datasets")
    async def list_datasets(search: str = ""):
        from eval_mcp.core.user_storage import list_datasets_from_db
        entries = list_datasets_from_db(_viewer_user(), search)
        return {"datasets": [{k: v for k, v in e.items() if k != "tests"} for e in entries]}

    @app.get("/api/datasets/{dataset_id}")
    async def get_dataset_detail(dataset_id: str, offset: int = 0, limit: int = 50):
        from eval_mcp.core.user_storage import get_dataset_from_db
        data = get_dataset_from_db(_viewer_user(), dataset_id)
        if not data:
            raise HTTPException(status_code=404, detail="Dataset not found")
        tests = data.get("tests", [])
        total = len(tests)
        if limit <= 0 or limit > 500:
            limit = 50
        if offset < 0:
            offset = 0
        return {
            "id": data["id"],
            "name": data.get("name", ""),
            "source": data.get("source", {"kind": "imported"}),
            "created_at": data["created_at"],
            "updated_at": data.get("updated_at"),
            "total": total,
            "offset": offset,
            "limit": limit,
            "tests": tests[offset : offset + limit],
        }

    @app.delete("/api/datasets/{dataset_id}")
    async def delete_dataset(dataset_id: str):
        from eval_mcp.core.user_storage import delete_dataset_from_db
        if not delete_dataset_from_db(_viewer_user(), dataset_id):
            raise HTTPException(status_code=404, detail="Dataset not found")
        return {"deleted": True}

    @app.patch("/api/datasets/{dataset_id}")
    async def patch_dataset(dataset_id: str, patch: _DatasetPatch):
        from eval_mcp.core.user_storage import update_dataset_in_db
        updated = update_dataset_in_db(
            _viewer_user(), dataset_id, name=patch.name, tests=patch.tests,
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Dataset not found")
        return {
            "id": updated["id"],
            "name": updated.get("name", ""),
            "source": updated.get("source", {"kind": "imported"}),
            "created_at": updated["created_at"],
            "updated_at": updated.get("updated_at"),
            "total": len(updated.get("tests", [])),
        }

    @app.get("/api/datasets/{dataset_id}/export")
    async def export_dataset_csv(dataset_id: str):
        from eval_mcp.core.user_storage import get_dataset_from_db
        data = get_dataset_from_db(_viewer_user(), dataset_id)
        if not data:
            raise HTTPException(status_code=404, detail="Dataset not found")
        tests = data.get("tests", [])
        keys: list[str] = []
        seen: set[str] = set()
        for t in tests:
            for k in (t.get("vars") or {}).keys():
                if k not in seen:
                    seen.add(k)
                    keys.append(k)
        for col in ("golden_answer", "question"):
            if col in keys:
                keys.remove(col)
                keys.insert(0, col)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(keys)
        for t in tests:
            v = t.get("vars") or {}
            writer.writerow([v.get(k, "") for k in keys])
        csv_bytes = buf.getvalue().encode("utf-8")
        safe_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in (data.get("name") or "dataset"))[:80]
        return StreamingResponse(
            iter([csv_bytes]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}.csv"'},
        )

    @app.get("/api/judges")
    async def list_judges(search: str = ""):
        from eval_mcp.core.user_storage import list_judges_from_db
        entries = list_judges_from_db(_viewer_user(), search)
        return {
            "judges": [
                {
                    "id": e["id"],
                    "name": e.get("name", ""),
                    "domain": (e.get("config") or {}).get("domain", "general"),
                    "criteria": [c.get("name") for c in (e.get("config") or {}).get("criteria", [])],
                    "created_at": e.get("created_at"),
                }
                for e in entries
            ]
        }

    @app.get("/api/judges/{judge_id}")
    async def get_judge_detail(judge_id: str):
        from eval_mcp.core.user_storage import get_judge_from_db
        data = get_judge_from_db(_viewer_user(), judge_id)
        if not data:
            raise HTTPException(status_code=404, detail="Judge not found")
        return data

    @app.delete("/api/judges/{judge_id}")
    async def delete_judge(judge_id: str):
        from eval_mcp.core.user_storage import delete_judge_from_db
        if not delete_judge_from_db(_viewer_user(), judge_id):
            raise HTTPException(status_code=404, detail="Judge not found")
        return {"deleted": True}

    @app.get("/api/documents/list")
    async def list_documents_local():
        # Local viewer has no S3, so always reads the per-user disk store.
        try:
            from eval_mcp.core.user_storage import list_user_document_paths
            paths = list_user_document_paths(_viewer_user())
            return {"documents": [{"path": p} for p in paths], "storage": "disk"}
        except Exception:
            return {"documents": [], "storage": "disk"}

    # Serve static files
    if STATIC_DIR.exists():
        @app.get("/results")
        async def results_page():
            return FileResponse(STATIC_DIR / "results.html")

        @app.get("/data")
        async def data_page():
            # The Next static export emits one .html per route.
            return FileResponse(STATIC_DIR / "data.html")

        @app.get("/chat")
        async def chat_page():
            # Local viewer has no chat backend — the shell still renders so
            # you can preview layout/styling, but message sending and session
            # listing will fail. The chat nav entry stays hidden in viewer
            # mode; this route is only hit via direct URL.
            return FileResponse(STATIC_DIR / "chat.html")

        @app.get("/history")
        async def history_page():
            # Same caveat as /chat — no Postgres locally, so the session list
            # will be empty. Layout is still inspectable.
            return FileResponse(STATIC_DIR / "history.html")

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
