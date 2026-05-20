"""FastAPI backend for the chat frontend."""

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.core.agent import Agent
from eval_mcp.core.bedrock_client import BedrockClient
from backend.core.database import Database
from backend.core.mcp_client import MultiMCPClient
from eval_mcp.core.s3_client import (
    is_s3_enabled,
    generate_presigned_upload_url,
    list_user_s3_documents,
    get_s3_document_content,
)
from eval_mcp.core.user_storage import save_document

# Configure logging at module level (runs when uvicorn loads the app)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [%(name)s] %(message)s',
    handlers=[logging.StreamHandler()]
)

# Create logger
logger = logging.getLogger(__name__)


def _user_safe_error(context: str) -> tuple[str, str]:
    """Log the active exception with a correlation id, return (id, safe message).

    Use inside `except` blocks anywhere we'd otherwise put `str(e)` into a
    response. The full traceback goes to logs; the client sees only the
    correlation id plus the exception class name. The class name is safe
    to leak (Python's standard exception taxonomy) and is enormously
    useful for triage — distinguishing "BotoCoreError" from "ValueError"
    tells us whether to look at AWS state or our own data shape. Users
    can quote the id when contacting support.
    """
    import sys as _sys
    error_id = uuid.uuid4().hex[:8]
    exc = _sys.exc_info()[1]
    exc_type = type(exc).__name__ if exc else "Unknown"
    logger.exception("[error_id=%s] %s", error_id, context)
    return error_id, f"{exc_type} (ref: {error_id})"


# Global clients (initialized on startup)
mcp_client: Optional[MultiMCPClient] = None
bedrock_client: Optional[BedrockClient] = None
db: Optional[Database] = None

# Supported file types for document upload (formats Claude can read)
SUPPORTED_DOCUMENT_TYPES = {
    # Extension -> MIME type
    "csv": "text/csv",
    "json": "application/json",
    "jsonl": "application/jsonlines",
    "pdf": "application/pdf",
    "txt": "text/plain",
    "md": "text/markdown",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

# File types that contain QA pairs (processed like CSV datasets)
QA_DATASET_TYPES = {"csv", "json", "jsonl"}


async def get_current_user_id(request: Request) -> str:
    """Extract user ID from oauth2-proxy header."""
    user_id = request.headers.get("X-Forwarded-User")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


# Session-based agents (one agent per chat session)
session_agents: Dict[str, Agent] = {}

# Active background tasks for agent processing (keyed by session_id)
# These run to completion even if client disconnects
active_tasks: Dict[str, asyncio.Task] = {}

# Event queues for streaming to clients (keyed by session_id)
event_queues: Dict[str, asyncio.Queue] = {}

# Sessions marked for cancellation
cancelled_sessions: Dict[str, dict] = {}  # session_id -> cancel info (evalId, configName)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle: startup and shutdown."""
    global mcp_client, bedrock_client, db

    # Startup
    print("🚀 Initializing backend...")

    try:
        # Get AWS region from environment
        region = os.getenv("AWS_REGION", "us-west-2")

        # Initialize database
        db = Database()
        await db.initialize()
        print("  ✓ Database initialized")

        # Initialize MCP client (connects to existing MCP servers)
        mcp_client = MultiMCPClient(region=region)
        await mcp_client.connect()
        print("  ✓ Connected to MCP servers")

        # Initialize Bedrock client
        bedrock_client = BedrockClient(region=region)
        print("  ✓ Bedrock client initialized")

        print("✓ Backend ready\n")

        yield  # Application runs here

    except Exception as e:
        print(f"❌ ERROR during startup: {e}")
        if mcp_client:
            try:
                await mcp_client.disconnect()
            except Exception:
                pass
        raise

    finally:
        # Shutdown
        import logging
        import traceback
        import sys

        logger = logging.getLogger(__name__)

        print("\n🔄 Shutting down backend...")
        logger.critical("[SHUTDOWN] Backend shutdown initiated")
        logger.critical(f"[SHUTDOWN] Stack trace:\n{traceback.format_stack()}")

        # Check if this is an exception-based shutdown
        exc_info = sys.exc_info()
        if exc_info[0] is not None:
            logger.critical(f"[SHUTDOWN] Shutdown caused by exception: {exc_info[0].__name__}: {exc_info[1]}")
        else:
            logger.critical("[SHUTDOWN] Normal shutdown (no exception)")

        # Wait for active agent tasks to complete (graceful shutdown)
        if active_tasks:
            print(f"  ⏳ Waiting for {len(active_tasks)} active agent task(s) to complete...")
            logger.info(f"[SHUTDOWN] Waiting for {len(active_tasks)} active tasks")
            try:
                # Wait up to 60 seconds for tasks to complete
                pending = list(active_tasks.values())
                done, pending = await asyncio.wait(pending, timeout=60)
                if pending:
                    print(f"  ⚠ {len(pending)} task(s) did not complete in time, cancelling...")
                    for task in pending:
                        task.cancel()
                else:
                    print(f"  ✓ All agent tasks completed")
            except Exception as e:
                print(f"  ⚠ Error waiting for tasks: {e}")
                logger.error(f"[SHUTDOWN] Task wait error: {e}")

        # Wait for running evaluations to complete
        try:
            from eval_mcp.tools.run_eval import (
                _running_evaluations,
                cancel_user_evaluation,
            )
            if _running_evaluations:
                print(f"  ⏳ Waiting for {len(_running_evaluations)} running evaluation(s) to complete...")
                logger.info(f"[SHUTDOWN] Waiting for {len(_running_evaluations)} running evals")
                # Give evals up to 4 minutes to finish, then cancel + export partial results
                for user_id in list(_running_evaluations.keys()):
                    try:
                        entry = _running_evaluations.get(user_id)
                        process = entry["process"] if entry else None
                        if process and process.returncode is None:
                            # Wait up to 4 min for eval to complete (leaves 1 min for cleanup)
                            await asyncio.wait_for(process.wait(), timeout=240)
                            print(f"    ✓ Evaluation for user {user_id[:8]}... completed")
                    except asyncio.TimeoutError:
                        print(f"    ⚠ Evaluation for user {user_id[:8]}... timed out, cancelling...")
                        await cancel_user_evaluation(user_id)
                    except Exception as e:
                        logger.warning(f"[SHUTDOWN] Error waiting for eval {user_id}: {e}")
                print("  ✓ All evaluations handled")
        except ImportError:
            pass  # Module not loaded
        except Exception as e:
            print(f"  ⚠ Error handling running evaluations: {e}")
            logger.error(f"[SHUTDOWN] Eval shutdown error: {e}")

        if mcp_client:
            try:
                await mcp_client.disconnect()
                print("  ✓ Disconnected from MCP servers")
            except Exception as e:
                print(f"  ⚠ Error disconnecting MCP client: {e}")
                logger.error(f"[SHUTDOWN] MCP disconnect error: {e}")

        if db:
            try:
                await db.close()
                print("  ✓ Database connections closed")
            except Exception as e:
                print(f"  ⚠ Error closing database: {e}")
                logger.error(f"[SHUTDOWN] DB close error: {e}")

        print("✓ Backend shutdown complete")
        logger.critical("[SHUTDOWN] Backend shutdown complete")


# Create FastAPI app with lifespan
app = FastAPI(
    title="Eval Platform Backend",
    lifespan=lifespan
)

# CORS middleware - restrict to APP_URL origin (defense in depth)
_app_url = os.environ.get("APP_URL", "")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_app_url] if _app_url else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class FileAttachment(BaseModel):
    name: str
    content: str
    type: str


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    stream: bool = True  # Enable streaming by default
    file: Optional[FileAttachment] = None  # Optional file attachment (CSV dataset)


class ChatResponse(BaseModel):
    response: str
    session_id: str


# Tool schema for Claude to identify JSON structure paths
STRUCTURE_MAPPING_TOOL = {
    "name": "submit_structure",
    "description": "Submit the identified paths to question and answer fields in the JSON structure.",
    "input_schema": {
        "type": "object",
        "properties": {
            "array_path": {
                "type": "string",
                "description": "Path to the array of items. Empty string if items are at root level. Example: 'conversationTurns' or 'data.items'",
            },
            "question_path": {
                "type": "string",
                "description": "Path to question field within each item. Example: 'question' or 'prompt.content[0].text'",
            },
            "answer_path": {
                "type": "string",
                "description": "Path to answer field within each item. Example: 'answer' or 'referenceResponses[0].content[0].text'",
            },
        },
        "required": ["question_path", "answer_path"],
    },
}


def _detect_agent_image(message: str) -> str | None:
    """Detect a container image URI in a user message.

    Matches ECR, DockerHub, GHCR, and other registry patterns.
    Returns the image URI or None.
    """
    if not message:
        return None
    # Common container registry patterns
    indicators = [".dkr.ecr.", "docker.io/", "ghcr.io/", "gcr.io/", "public.ecr.aws/"]
    for word in message.split():
        # Strip surrounding quotes/backticks
        clean = word.strip("`\"'<>()[]")
        if any(ind in clean for ind in indicators):
            return clean
        # Match image:tag pattern like myregistry.com/org/image:tag
        if "/" in clean and (":" in clean or "." in clean.split("/")[0]):
            parts = clean.split("/")
            if len(parts) >= 2 and "." in parts[0] and not parts[0].startswith("http"):
                return clean
    return None


def _get_by_path(obj, path: str):
    """Extract value from nested dict/list using dot notation with array indices.

    Examples: 'field', 'field.sub', 'field[0]', 'field[0].sub', 'content[0].text'
    """
    import re

    if not path:
        return obj

    current = obj
    # Parse path into tokens: split by . but keep [n] attached to field names
    tokens = re.findall(r'[^.\[\]]+|\[\d+\]', path)

    i = 0
    while i < len(tokens) and current is not None:
        token = tokens[i]

        if token.startswith('[') and token.endswith(']'):
            # Array index
            idx = int(token[1:-1])
            if isinstance(current, list) and idx < len(current):
                current = current[idx]
            else:
                return None
        else:
            # Field name
            if isinstance(current, dict) and token in current:
                current = current[token]
            else:
                return None
        i += 1

    return current


def _extract_qa_from_structure(data, array_path: str, question_path: str, answer_path: str) -> list:
    """Extract QA pairs from data using paths identified by Claude."""

    # Get the array of items
    if array_path:
        items = _get_by_path(data, array_path)
    else:
        items = data if isinstance(data, list) else [data]

    if not isinstance(items, list):
        return []

    qa_pairs = []
    for item in items:
        question = _get_by_path(item, question_path)
        answer = _get_by_path(item, answer_path)

        if question and answer:
            qa_pairs.append({
                "question": str(question).strip(),
                "golden_answer": str(answer).strip(),
            })

    return qa_pairs


async def _deduce_structure_with_claude(content_str: str, filename: str) -> dict | None:
    """Use Claude to identify JSON structure paths for question/answer fields.

    Returns:
        {"array_path": str, "question_path": str, "answer_path": str} or None
    """
    import asyncio

    if not bedrock_client:
        return None

    # Truncate for prompt
    sample = content_str[:4000]

    prompt = f"""Analyze this data and identify the paths to extract question-answer pairs.

File: {filename}
Data sample:
{sample}

Identify:
1. array_path: Where is the array of items? (empty if root is array)
2. question_path: Path to question/input/prompt within each item
3. answer_path: Path to answer/response/output within each item

Use dot notation for nested fields, [0] for array indices.
Example for nested: array_path="conversationTurns", question_path="prompt.content[0].text", answer_path="referenceResponses[0].content[0].text"
Example for flat: array_path="", question_path="question", answer_path="answer"

Submit using submit_structure tool."""

    try:
        response = await asyncio.to_thread(
            bedrock_client.create_message,
            messages=[{"role": "user", "content": prompt}],
            tools=[STRUCTURE_MAPPING_TOOL],
            tool_choice={"type": "auto"},
            max_tokens=256,
        )

        tool_uses = bedrock_client.extract_tool_uses(response)
        if tool_uses:
            result = tool_uses[0]["input"]
            if result.get("question_path") and result.get("answer_path"):
                logger.info(f"Claude identified structure: {result}")
                return {
                    "array_path": result.get("array_path", ""),
                    "question_path": result["question_path"],
                    "answer_path": result["answer_path"],
                }
        return None
    except Exception as e:
        logger.warning(f"Claude structure detection failed: {e}")
        return None


def _sample_content_for_analysis(content_str: str, filename: str, max_rows: int = 10) -> str:
    """Extract a small sample of file content for structure analysis.

    Only a few rows are needed to detect column names and structure.
    Sending the full file through MCP is unnecessarily slow.
    """
    ext = filename.lower().split(".")[-1] if "." in filename else "csv"

    if ext in ("jsonl", "ndjson"):
        lines = content_str.strip().split("\n")
        return "\n".join(lines[:max_rows])
    elif ext == "csv":
        lines = content_str.strip().split("\n")
        # Header + max_rows data rows
        return "\n".join(lines[:max_rows + 1])
    else:
        # JSON: must send full content since structure could be nested
        # but JSON files are typically smaller than JSONL/CSV
        return content_str


async def process_qa_dataset_content(
    mcp: "MultiMCPClient",
    content: bytes,
    filename: str,
    user_id: str,
) -> dict:
    """Process QA dataset content (CSV, JSON, JSONL) - analyze and save as YAML dataset.

    Args:
        mcp: MCP client for calling dataset tools
        content: Raw file content as bytes
        filename: Original filename (used to detect format)
        user_id: User ID for storage isolation

    Returns:
        Dict with:
        - success: bool
        - message: str (for agent)
        - path: str (saved YAML path, if successful)
        - rows_saved: int (if successful)
        - error: str (if failed)
    """
    from eval_mcp.core.user_storage import save_dataset_to_db
    from eval_mcp.tools.save_dataset import parse_content_to_rows, rows_to_test_cases, generate_dataset_name

    logger = logging.getLogger(__name__)
    logger.info(f"Processing QA dataset: {filename} for user {user_id}")

    try:
        # Decode bytes to string
        content_str = content.decode("utf-8")

        # Step 1: Analyze structure using only a sample (avoid sending full file through MCP)
        sample_str = _sample_content_for_analysis(content_str, filename)
        analyze_result = await mcp.call_tool("analyze_dataset", {
            "file_content": sample_str,
            "filename": filename,
            "user_id": user_id,
        })

        # Extract text from MCP result
        if hasattr(analyze_result, "content") and analyze_result.content:
            analysis_text = analyze_result.content[0].text
        else:
            analysis_text = str(analyze_result)

        analysis = json.loads(analysis_text)

        if not analysis.get("success"):
            error = analysis.get("error", "Unknown error")
            return {
                "success": False,
                "message": f"[Dataset upload failed: {error}]",
                "error": error,
            }

        # Extract from nested structure if present (server_http.py wraps in "analysis" key)
        analysis_data = analysis.get("analysis", analysis)
        column_mapping = analysis_data.get("column_mapping", {})

        # If auto-detection failed, use Claude to identify structure
        if not analysis_data.get("valid"):
            logger.info(f"Auto-detection failed for {filename}, fields={analysis_data.get('fields', [])}, using Claude to identify structure")

            structure = await _deduce_structure_with_claude(sample_str, filename)
            logger.info(f"Claude returned structure: {structure}")

            if structure:
                # Parse full content and extract using Claude's paths
                ext = filename.lower().split(".")[-1] if "." in filename else ""
                try:
                    qa_pairs = []
                    if ext == "json":
                        data = json.loads(content_str)
                        qa_pairs = _extract_qa_from_structure(
                            data,
                            structure["array_path"],
                            structure["question_path"],
                            structure["answer_path"],
                        )
                    elif ext in ("jsonl", "ndjson"):
                        for line in content_str.strip().split("\n"):
                            if line.strip():
                                line_data = json.loads(line)
                                line_pairs = _extract_qa_from_structure(
                                    line_data,
                                    structure["array_path"],
                                    structure["question_path"],
                                    structure["answer_path"],
                                )
                                qa_pairs.extend(line_pairs)

                    logger.info(f"Extracted {len(qa_pairs)} QA pairs from {filename}")

                    if qa_pairs:
                        base_name = Path(filename).stem
                        dataset_name = generate_dataset_name(base_name)
                        test_cases = [{"vars": {"question": p["question"], "golden_answer": p["golden_answer"]}} for p in qa_pairs]
                        dataset_id = save_dataset_to_db(
                            user_id,
                            dataset_name,
                            test_cases,
                            source={"kind": "imported", "origin": filename},
                        )

                        return {
                            "success": True,
                            "message": f"[Dataset uploaded: '{filename}' saved as '{dataset_name}' with {len(qa_pairs)} QA pairs]",
                            "dataset": dataset_name,
                            "dataset_id": dataset_id,
                            "rows_saved": len(qa_pairs),
                        }
                except Exception as e:
                    logger.error(f"Structure extraction failed: {e}")

            # Claude couldn't figure it out
            fields = analysis_data.get("fields", [])
            return {
                "success": False,
                "message": f"[Dataset '{filename}': Could not detect question/answer fields. Fields found: {fields}]",
                "error": "Column detection failed",
            }

        # Validate we have flat mapping
        if not column_mapping.get("question") or not column_mapping.get("golden_answer"):
            issues = analysis_data.get("issues", ["Missing question or answer column"])
            return {
                "success": False,
                "message": f"[Dataset '{filename}' has issues: {'; '.join(issues)}]",
                "error": "Column mapping incomplete",
                "issues": issues,
            }

        # Step 2: Save the dataset locally (parse full content + save to DB directly)
        rows = parse_content_to_rows(content_str, filename)
        test_cases = rows_to_test_cases(rows, column_mapping["question"], column_mapping["golden_answer"])

        if not test_cases:
            return {
                "success": False,
                "message": f"[Dataset save failed: No valid rows found with both question and answer]",
                "error": "No valid rows found",
            }

        dataset_name = generate_dataset_name(Path(filename).stem)
        dataset_id = save_dataset_to_db(
            user_id,
            dataset_name,
            test_cases,
            source={"kind": "imported", "origin": filename},
        )

        return {
            "success": True,
            "message": f"[Dataset uploaded successfully: '{filename}' saved as '{dataset_name}' with {len(test_cases)} QA pairs]",
            "dataset": dataset_name,
            "dataset_id": dataset_id,
            "rows_saved": len(test_cases),
        }

    except Exception:
        error_id, safe_msg = _user_safe_error("Dataset processing")
        return {
            "success": False,
            "message": f"[Dataset processing failed: {safe_msg}]",
            "error": safe_msg,
            "error_id": error_id,
        }


@app.post("/api/chat/message")
async def chat(request: ChatRequest, user_id: str = Depends(get_current_user_id)):
    """Handle chat messages with optional streaming."""
    if request.stream:
        # Return SSE stream
        return StreamingResponse(
            chat_stream(request, user_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            }
        )
    else:
        # Return regular JSON response
        return await chat_non_stream(request, user_id)


@app.get("/api/chat/status/{session_id}")
async def chat_status(session_id: str, user_id: str = Depends(get_current_user_id)):
    """Check if a chat session is currently processing."""
    if session_id in active_tasks and not active_tasks[session_id].done():
        return {"running": True}
    return {"running": False}


async def _cancel_eval_subprocess_and_reconnect(user_id: str) -> None:
    """Background cleanup after a chat cancel: same path regardless of
    whether an eval subprocess was running.

    The previous "skip reconnect when no eval was running" optimization
    created two different code paths for plain-chat vs eval cancels,
    which the user pushed back on. Unified behavior here — always do
    both pieces (SIGTERM, reconnect MCP) — and rely on the
    ``_reconnect_lock`` sync in ``MCPClient.list_tools`` to make the
    inevitable race against the next message's list_tools safe (the
    list_tools call simply waits for the reconnect to finish).

    Runs as a background task so the Stop HTTP response returns
    instantly. Cost: the user's next message may briefly wait for the
    reconnect to drain (~1-2s typical, up to ~7s if an eval subprocess
    is going through its SIGTERM grace period).
    """
    try:
        mcp_url = os.environ["EVAL_MCP_URL"]
        base_url = mcp_url.replace("/mcp", "")
        # Short timeout: the MCP cancel endpoint just SIGTERMs the
        # subprocess and returns; the actual subprocess wait happens
        # async inside the MCP server. We don't need to wait long here.
        async with httpx.AsyncClient() as client:
            await client.post(f"{base_url}/cancel/{user_id}", timeout=2.0)
        # Cap reconnect attempts to 3 so a transient MCP unavailability
        # doesn't pile up retries (default is 10 → up to ~9 minutes of
        # _reconnect_lock held → frontend cooldown can't compensate).
        await mcp_client.reconnect_server("eval", max_retries=3)
    except Exception as e:
        logger.warning(f"[CANCEL bg] Failed to clean up eval state for {user_id}: {e}")


@app.post("/api/chat/cancel/{session_id}")
async def cancel_chat(session_id: str, user_id: str = Depends(get_current_user_id)):
    """Cancel an ongoing chat request and any running evaluation.

    The task may live on a different pod (ALB stickiness isn't reliable
    end-to-end through CloudFront). We DON'T early-return when the
    task isn't local — the DB write below is the cross-pod signal that
    reaches whichever pod is actually running the agent; without it,
    that pod's SSE stream never closes and the user sees "Stopping…"
    forever. The previous code had an early-return above the DB write,
    which made the cross-pod path silently no-op — the exact bug
    c222fee was meant to fix but didn't fully wire up.
    """
    global cancelled_sessions, active_tasks

    local_task: Optional[asyncio.Task] = active_tasks.get(session_id)
    local_task_runnable = local_task is not None and not local_task.done()

    # Read eval info from THIS pod's MCP sidecar. Returns
    # {"running": False, ...} if the eval is on another pod — that's
    # fine; the other pod will surface the resume hint via its own
    # CancelledError handler when it processes the cross-pod cancel.
    eval_info: Dict[str, Any] = {}
    try:
        mcp_url = os.environ["EVAL_MCP_URL"]
        base_url = mcp_url.replace("/mcp", "")
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{base_url}/eval-info/{user_id}", timeout=1.0)
            eval_info = resp.json()
            logger.info(f"[CANCEL] Eval info: {eval_info}")
    except Exception as e:
        logger.warning(f"[CANCEL] Failed to get eval info: {e}")

    cancelled_sessions[session_id] = eval_info

    # ALWAYS write the cross-pod signal — the task may live on a
    # different pod, and that pod needs to see the cancel via its
    # 500ms DB poll in run_agent_background.
    try:
        await asyncio.wait_for(
            db.mark_session_cancelled(session_id, json.dumps(eval_info)),
            timeout=2.0,
        )
    except (asyncio.TimeoutError, Exception) as e:
        logger.warning(f"[CANCEL] Failed to mark session cancelled in DB: {e}")

    # task.cancel() is what stops the streaming response when the
    # task is local. Cross-pod, the DB-poll path in
    # run_agent_background does the equivalent within ~500ms.
    if local_task_runnable:
        local_task.cancel()

    # Fire-and-forget local MCP cancel + reconnect. If the eval is in
    # this pod's sidecar this SIGTERMs the subprocess. If it's
    # elsewhere this is a no-op — the right pod's agent loop fires
    # its own local cancel when it picks up the DB row.
    asyncio.create_task(_cancel_eval_subprocess_and_reconnect(user_id))

    logger.info(
        f"[CANCEL] User {user_id} cancelled session {session_id} "
        f"(local_task={local_task_runnable})"
    )

    return {"success": True, "message": "Cancellation requested", **eval_info}


async def run_agent_background(
    session_id: str,
    user_id: str,
    final_message: str,
    user_message_for_db: str,
    queue: asyncio.Queue,
    logger: logging.Logger,
):
    """
    Background worker that runs agent to completion, regardless of client connection.

    Puts events into queue for SSE streaming. Saves response to DB when complete.
    """
    global session_agents, active_tasks, cancelled_sessions

    full_response = ""
    event_count = 0
    was_cancelled = False

    try:
        # Get agent for this session
        agent = session_agents.get(session_id)
        if not agent:
            await queue.put({"type": "error", "data": {"error": "Agent not found"}})
            await queue.put(None)  # Signal completion
            return

        # Save user message to DB
        user_msg_id = str(uuid.uuid4())
        await db.save_message(user_msg_id, session_id, "user", user_message_for_db)

        # Clear any stale cross-pod cancellation flag — a new turn is
        # starting, the user wants this one to run. Without this, a
        # previous Stop's row in session_cancellations would make the
        # very first iteration of THIS new turn immediately cancel.
        try:
            await asyncio.wait_for(
                db.clear_session_cancellation(session_id), timeout=2.0
            )
        except (asyncio.TimeoutError, Exception):
            pass

        logger.info(f"[AGENT START] Starting agent loop for session {session_id}")

        # Throttle the cross-pod cancellation poll so we don't hammer
        # the DB on every streamed text token (the async for below
        # yields hundreds of events per second during Bedrock
        # streaming). 500ms is fast enough to make Stop feel
        # responsive but lets us read the flag at most twice/sec/pod.
        import time as _time
        last_xpod_poll = 0.0
        XPOD_POLL_INTERVAL = 0.5

        async for event in agent.run_conversation_turn_streaming(final_message):
            # Check for cancellation — first the cheap in-memory check
            # (handles the case where cancel landed on THIS pod), then
            # the DB-backed cross-pod check (handles the case where
            # cancel landed on a different pod).
            cancel_info: Optional[Dict[str, Any]] = None
            if session_id in cancelled_sessions:
                cancel_info = cancelled_sessions[session_id]
            else:
                now = _time.monotonic()
                if now - last_xpod_poll >= XPOD_POLL_INTERVAL:
                    last_xpod_poll = now
                    try:
                        row = await asyncio.wait_for(
                            db.get_session_cancellation(session_id), timeout=1.0
                        )
                        if row is not None:
                            try:
                                cancel_info = json.loads(row.get("eval_info") or "{}") or {}
                            except json.JSONDecodeError:
                                cancel_info = {}
                            # Mirror into local dict so subsequent
                            # checks short-circuit on the cheap path.
                            cancelled_sessions[session_id] = cancel_info
                    except (asyncio.TimeoutError, Exception):
                        pass

            if cancel_info is not None:
                logger.info(f"[AGENT CANCELLED] Session {session_id} cancelled by user, eval info: {cancel_info}")
                was_cancelled = True

                # On EKS the cancel HTTP request can land on a different
                # pod than the one running this task (CloudFront → ALB
                # stickiness isn't reliable end-to-end). In that case the
                # OTHER pod's cancel_chat already POSTed to ITS local
                # /cancel/{user_id} — a no-op, because the Inspect
                # subprocess and its _running_evaluations entry live in
                # THIS pod's MCP sidecar. Fire the local cancel here so
                # the subprocess actually dies. Safe to call even when
                # cancel landed on this pod (same-pod cancel_chat already
                # fired it; second call is harmless — the MCP endpoint
                # is a no-op when nothing is registered).
                asyncio.create_task(_cancel_eval_subprocess_and_reconnect(user_id))

                await queue.put({"type": "cancelled", "data": {"message": "Request cancelled", **cancel_info}})
                break

            event_count += 1
            event_type = event['type']
            logger.debug(f"[EVENT {event_count}] Type: {event_type}")

            # Put event in queue for SSE delivery
            await queue.put(event)

            # Collect full response text
            if event_type == 'text':
                full_response += event['data'].get('content', '')
            elif event_type == 'complete':
                full_response = event['data'].get('response', '')
                logger.info(f"[AGENT COMPLETE] Session {session_id}, events: {event_count}")

        # Save assistant message to DB (even partial if cancelled).
        # Bounded by timeout — see the matching block in the
        # CancelledError handler below for why.
        if full_response:
            if was_cancelled:
                full_response += "\n\n*[Response cancelled by user]*"
                cancel_info = cancelled_sessions.get(session_id, {})
                eval_id = cancel_info.get("evalId")
                if eval_id:
                    agent = session_agents.get(session_id)
                    if agent:
                        agent.cancel_info = cancel_info
            assistant_msg_id = str(uuid.uuid4())
            try:
                await asyncio.wait_for(
                    db.save_message(assistant_msg_id, session_id, "assistant", full_response),
                    timeout=5.0,
                )
                logger.info(f"[DB SAVE] Saved assistant response for session {session_id}")
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"[DB SAVE] Failed/timed out saving response: {e}")

            # Update session title if this is the first message
            try:
                messages = await asyncio.wait_for(
                    db.get_session_messages(session_id), timeout=2.0
                )
                if len(messages) == 2:
                    title = (
                        user_message_for_db[:50] + "..."
                        if len(user_message_for_db) > 50
                        else user_message_for_db
                    )
                    await asyncio.wait_for(
                        db.update_session_title(session_id, title), timeout=2.0
                    )
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"[DB] Failed/timed out updating title: {e}")

    except asyncio.CancelledError:
        # Task was cancelled via Stop button - this is expected
        cancel_info = cancelled_sessions.get(session_id, {})
        eval_id = cancel_info.get("evalId")
        logger.info(f"[AGENT CANCELLED] Session {session_id} task cancelled, evalId={eval_id}")
        was_cancelled = True
        await queue.put({"type": "cancelled", "data": {"message": "Request cancelled", **cancel_info}})

        # Store cancel info on agent so _fix_orphaned_tool_uses includes it in the tool result
        agent = session_agents.get(session_id)
        if agent and eval_id:
            agent.cancel_info = cancel_info

        # Save partial response to DB — but bounded by a timeout. The
        # SSE stream can't close until this except handler returns
        # (finally puts None on queue), so a slow DB save would leave
        # the user's frontend hung indefinitely on the streaming reader,
        # with isLoading/isCancelling stuck true. Symptom: "if i restart
        # the page after stopping it works" — only a fresh React state
        # unblocks the user. 5s is generous; the DB save normally takes
        # <50ms.
        if full_response:
            full_response += "\n\n*[Response cancelled by user]*"
            assistant_msg_id = str(uuid.uuid4())
            try:
                await asyncio.wait_for(
                    db.save_message(assistant_msg_id, session_id, "assistant", full_response),
                    timeout=5.0,
                )
                logger.info(f"[DB SAVE] Saved partial response for cancelled session {session_id}")
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"[DB SAVE] Failed/timed out saving cancelled partial: {e}")

    except Exception:
        error_id, safe_msg = _user_safe_error(f"Agent background task session={session_id}")
        await queue.put({"type": "error", "data": {"error": safe_msg, "error_id": error_id}})

    finally:
        # Signal completion to queue readers FIRST — the SSE stream
        # needs to close so the frontend can re-enable input. Anything
        # else in finally that could block (cancelled_sessions cleanup
        # is in-memory and instant, so safe to run after) must not
        # delay this. queue.put on an asyncio.Queue is in-memory, but
        # we wrap defensively anyway.
        try:
            await asyncio.wait_for(queue.put(None), timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning(f"[BACKGROUND END] queue.put(None) timed out for {session_id}")

        # Cleanup
        if session_id in active_tasks:
            del active_tasks[session_id]
        if session_id in event_queues:
            del event_queues[session_id]
        cancelled_sessions.pop(session_id, None)

        logger.info(f"[BACKGROUND END] Session {session_id}, total events: {event_count}, cancelled: {was_cancelled}")


async def chat_stream(request: ChatRequest, user_id: str):
    """Stream chat responses with progress updates via SSE."""
    global session_agents, active_tasks, event_queues

    logger = logging.getLogger(__name__)

    logger.info(f"[STREAM START] Session: {request.session_id}, User: {user_id}, Message length: {len(request.message)}")

    if not bedrock_client or not mcp_client or not db:
        logger.error("[STREAM ERROR] Backend not initialized")
        yield f"event: error\ndata: {json.dumps({'error': 'Backend not initialized'})}\n\n"
        return

    # Generate session ID if not provided
    session_id = request.session_id or str(uuid.uuid4())

    # Set user_id on MCP client for auto-injection into tool calls
    mcp_client.set_user_id(user_id)

    # Send session ID immediately
    yield f"event: session\ndata: {json.dumps({'session_id': session_id})}\n\n"

    # Check if there's already an active task for this session
    if session_id in active_tasks and not active_tasks[session_id].done():
        # Reconnecting client - read from existing queue
        logger.info(f"[RECONNECT] Client reconnected to active session {session_id}")
        queue = event_queues.get(session_id)
        if queue:
            try:
                while True:
                    event = await queue.get()
                    if event is None:
                        break
                    yield f"event: {event['type']}\ndata: {json.dumps(event['data'])}\n\n"
            except asyncio.CancelledError:
                logger.info(f"[RECONNECT CANCELLED] Client disconnected again from session {session_id}")
        return

    # Ensure user exists
    await db.create_user(user_id, user_id)

    # Create session if it doesn't exist
    await db.create_session(session_id, user_id)

    # Get or create agent for this session
    if session_id not in session_agents:
        agent = Agent(bedrock_client, mcp_client, debug=False)

        # Load existing conversation history from database
        existing_messages = await db.get_session_messages(session_id)
        if existing_messages:
            agent.conversation_history = [
                {"role": msg["role"], "content": msg["content"]}
                for msg in existing_messages
            ]

        session_agents[session_id] = agent

    # Process file upload directly via Dataset MCP if present
    file_result_message = ""
    if request.file:
        yield f"event: progress\ndata: {json.dumps({'message': f'Processing {request.file.name}...'})}\n\n"

        try:
            file_result = await process_file_upload(
                mcp_client,
                request.file,
                user_id,
            )
            file_result_message = file_result
            yield f"event: progress\ndata: {json.dumps({'message': 'File processed successfully'})}\n\n"
        except Exception:
            error_id, safe_msg = _user_safe_error("File processing")
            file_result_message = f"[File upload failed: {safe_msg}]"
            yield f"event: progress\ndata: {json.dumps({'message': f'File processing failed (ref: {error_id})'})}\n\n"

    # Build final message for agent
    if file_result_message:
        final_message = f"{request.message}\n\n{file_result_message}" if request.message else file_result_message
    else:
        final_message = request.message

    # Detect container image URIs in the message and inject agent eval context
    agent_image = _detect_agent_image(final_message)
    if agent_image:
        final_message += (
            f"\n\n[Agent container image detected: {agent_image}\n"
            "Use analyze_agent_image(agentImage=\""
            f"{agent_image}\") to automatically extract code, analyze tools/behavior, "
            "generate test cases, and create the eval config.\n"
            "Then run_evaluation(configName=...) to execute.\n"
            "Everything is handled automatically — no dataset or judge setup needed.]"
        )

    # Create queue for this session
    queue = asyncio.Queue()
    event_queues[session_id] = queue

    # Start background task - runs to completion regardless of client connection
    task = asyncio.create_task(
        run_agent_background(
            session_id=session_id,
            user_id=user_id,
            final_message=final_message,
            user_message_for_db=request.message,
            queue=queue,
            logger=logger,
        )
    )
    active_tasks[session_id] = task

    # Stream events from queue to client
    try:
        while True:
            event = await queue.get()
            if event is None:
                # Task completed
                break

            event_type = event['type']
            yield f"event: {event_type}\ndata: {json.dumps(event['data'])}\n\n"

    except asyncio.CancelledError:
        # Client disconnected - task continues in background
        logger.info(f"[CLIENT DISCONNECT] Client disconnected from session {session_id}, task continues in background")
        # Don't cleanup - task will do it when complete

    except Exception:
        error_id, safe_msg = _user_safe_error(f"Stream error session={session_id}")
        yield f"event: error\ndata: {json.dumps({'error': safe_msg, 'error_id': error_id})}\n\n"


async def chat_non_stream(request: ChatRequest, user_id: str) -> ChatResponse:
    """Handle non-streaming chat requests (legacy mode)."""
    global session_agents

    if not bedrock_client or not mcp_client or not db:
        raise HTTPException(status_code=500, detail="Backend not initialized")

    try:
        # Generate session ID if not provided
        session_id = request.session_id or str(uuid.uuid4())

        # Set user_id on MCP client for auto-injection into tool calls
        mcp_client.set_user_id(user_id)

        # Ensure user exists
        await db.create_user(user_id, user_id)

        # Create session if it doesn't exist
        await db.create_session(session_id, user_id)

        # Get or create agent for this session
        if session_id not in session_agents:
            agent = Agent(bedrock_client, mcp_client, debug=False)

            # Load existing conversation history from database
            existing_messages = await db.get_session_messages(session_id)
            if existing_messages:
                # Convert DB format to agent format (only role and content needed)
                agent.conversation_history = [
                    {"role": msg["role"], "content": msg["content"]}
                    for msg in existing_messages
                ]

            session_agents[session_id] = agent

        agent = session_agents[session_id]

        # Save user message
        user_msg_id = str(uuid.uuid4())
        await db.save_message(user_msg_id, session_id, "user", request.message)

        # Get agent response
        response = await agent.run_conversation_turn(request.message)

        # Save assistant message
        assistant_msg_id = str(uuid.uuid4())
        await db.save_message(assistant_msg_id, session_id, "assistant", response)

        # Update session title if this is the first message
        messages = await db.get_session_messages(session_id)
        if len(messages) == 2:  # First user + first assistant message
            # Use first 50 chars of user message as title
            title = request.message[:50] + "..." if len(request.message) > 50 else request.message
            await db.update_session_title(session_id, title)

        return ChatResponse(
            response=response,
            session_id=session_id,
        )
    except Exception:
        _, safe_msg = _user_safe_error("chat_non_stream endpoint")
        raise HTTPException(status_code=500, detail=safe_msg)


@app.get("/api/sessions")
async def get_sessions(user_id: str = Depends(get_current_user_id)):
    """Get chat sessions for the authenticated user."""
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")

    try:
        # Ensure user exists
        await db.create_user(user_id, user_id)

        sessions = await db.get_user_sessions(user_id)
        return {"sessions": sessions}
    except Exception:
        _, safe_msg = _user_safe_error("get_sessions endpoint")
        raise HTTPException(status_code=500, detail=safe_msg)


@app.get("/health")
async def health():
    """Liveness check - returns ok if process is running."""
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready():
    """Readiness check for Kubernetes."""
    return {"status": "ok"}


# ============== Document Upload ==============

@app.post("/api/documents/upload")
async def upload_documents(
    files: List[UploadFile] = File(...),
    user_id: str = Depends(get_current_user_id),
):
    """Upload documents for use as knowledge base.

    Accepts multiple files. Supported formats: CSV, PDF, TXT, MD, PNG, JPG, GIF, WEBP.
    - Single file: saved to documents/
    - Multiple files: saved to documents/{first_filename}_{timestamp}/
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    # Validate all files first
    validated_files = []
    unsupported = []

    for file in files:
        if not file.filename:
            continue

        # Get extension
        ext = file.filename.lower().split(".")[-1] if "." in file.filename else ""

        if ext not in SUPPORTED_DOCUMENT_TYPES:
            unsupported.append(file.filename)
        else:
            validated_files.append((file, ext))

    if unsupported:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file types: {', '.join(unsupported)}. Supported: {', '.join(SUPPORTED_DOCUMENT_TYPES.keys())}"
        )

    if not validated_files:
        raise HTTPException(status_code=400, detail="No valid files to upload")

    # Determine folder name for multiple files
    folder = None
    if len(validated_files) > 1:
        # Use first filename + timestamp
        first_filename = validated_files[0][0].filename
        name_without_ext = first_filename.rsplit(".", 1)[0] if "." in first_filename else first_filename
        # Sanitize: only alphanumeric, underscore, hyphen
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name_without_ext)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        folder = f"{safe_name}_{timestamp}"

    # Save files and process CSVs
    saved_files = []
    csv_results = []  # Results from CSV processing
    non_csv_files = []  # Files that aren't CSVs (for generate_qa_pairs)

    for file, ext in validated_files:
        content = await file.read()
        filepath = save_document(user_id, file.filename, content, folder=folder)
        saved_files.append(filepath.name)

        if ext in QA_DATASET_TYPES:
            # Process QA dataset files (CSV, JSON, JSONL) immediately
            dataset_result = await process_qa_dataset_content(
                mcp_client,
                content,
                file.filename,
                user_id,
            )
            csv_results.append({
                "filename": file.filename,
                **dataset_result,
            })
        else:
            # Non-dataset files go to generate_qa_pairs flow
            non_csv_files.append(filepath.name)

    logger.info(f"User {user_id} uploaded {len(saved_files)} files to {folder or 'documents/'}")

    return {
        "success": True,
        "folder": folder,
        "files": saved_files,
        "count": len(saved_files),
        "csv_results": csv_results if csv_results else None,
        "non_csv_files": non_csv_files if non_csv_files else None,
    }


# ============== S3 Pre-signed URL Upload (for large files) ==============

class PresignRequest(BaseModel):
    """Request body for presigned URL generation."""
    filename: str
    content_type: str
    folder: Optional[str] = None


class PresignResponse(BaseModel):
    """Response with presigned URL for upload."""
    upload_url: str
    s3_key: str
    bucket: str
    expires_in: int


@app.get("/api/documents/upload-mode")
async def get_upload_mode(user_id: str = Depends(get_current_user_id)):
    """Get the current upload mode (s3 or direct).

    Frontend uses this to determine whether to use presigned URLs or direct upload.
    """
    return {
        "mode": "s3" if is_s3_enabled() else "direct",
        "s3_enabled": is_s3_enabled(),
    }


@app.post("/api/documents/presign", response_model=PresignResponse)
async def get_presigned_upload_url(
    request: PresignRequest,
    user_id: str = Depends(get_current_user_id),
):
    """Get a pre-signed URL for uploading a file directly to S3.

    This bypasses ALB/CloudFront body size limits by having the browser
    upload directly to S3.

    Flow:
    1. Frontend calls this endpoint to get a presigned URL
    2. Frontend uploads file directly to S3 using the URL
    3. Frontend calls /api/documents/confirm to register the upload
    """
    if not is_s3_enabled():
        raise HTTPException(
            status_code=400,
            detail="S3 uploads not enabled. Use /api/documents/upload instead."
        )

    # Validate file type
    ext = request.filename.lower().split(".")[-1] if "." in request.filename else ""
    if ext not in SUPPORTED_DOCUMENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext}. Supported: {', '.join(SUPPORTED_DOCUMENT_TYPES.keys())}"
        )

    try:
        result = generate_presigned_upload_url(
            user_id=user_id,
            filename=request.filename,
            content_type=request.content_type,
            folder=request.folder,
        )
        return PresignResponse(**result)
    except Exception as e:
        logger.error(f"Failed to generate presigned URL: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate upload URL")


class ConfirmUploadRequest(BaseModel):
    """Request body to confirm S3 upload completion."""
    s3_keys: List[str]
    folder: Optional[str] = None


@app.post("/api/documents/confirm")
async def confirm_s3_upload(
    request: ConfirmUploadRequest,
    user_id: str = Depends(get_current_user_id),
):
    """Confirm that files have been uploaded to S3.

    Called by frontend after successfully uploading files via presigned URLs.
    This registers the uploads in the system for later use.
    """
    if not is_s3_enabled():
        raise HTTPException(status_code=400, detail="S3 uploads not enabled")

    # Validate that all keys belong to this user
    for key in request.s3_keys:
        if not key.startswith(f"users/{user_id}/"):
            raise HTTPException(status_code=403, detail="Access denied to S3 key")

    # Extract filenames from keys and process CSVs
    files = []
    csv_results = []
    non_csv_files = []

    for key in request.s3_keys:
        # Key format: users/{user_id}/documents/{folder?}/{timestamp}_{filename}
        filename = key.split("/")[-1]
        # Remove timestamp prefix (format: YYYYMMDD_HHMMSS_)
        if "_" in filename:
            parts = filename.split("_", 2)
            if len(parts) >= 3:
                filename = parts[2]
        files.append(filename)

        # Check if it's a QA dataset file and process it
        ext = filename.lower().split(".")[-1] if "." in filename else ""
        if ext in QA_DATASET_TYPES:
            try:
                # Read file content from S3
                content = get_s3_document_content(key)
                # Process it
                dataset_result = await process_qa_dataset_content(
                    mcp_client,
                    content,
                    filename,
                    user_id,
                )
                csv_results.append({
                    "filename": filename,
                    **dataset_result,
                })
            except Exception:
                error_id, safe_msg = _user_safe_error(f"Process {ext.upper()} {filename} from S3")
                csv_results.append({
                    "filename": filename,
                    "success": False,
                    "message": f"[Failed to process {ext.upper()}: {safe_msg}]",
                    "error": safe_msg,
                    "error_id": error_id,
                })
        else:
            non_csv_files.append(filename)

    logger.info(f"User {user_id} confirmed S3 upload of {len(files)} files")

    return {
        "success": True,
        "folder": request.folder,
        "files": files,
        "count": len(files),
        "s3_keys": request.s3_keys,
        "csv_results": csv_results if csv_results else None,
        "non_csv_files": non_csv_files if non_csv_files else None,
    }


@app.get("/api/documents/list")
async def list_documents(user_id: str = Depends(get_current_user_id)):
    """List all documents for the authenticated user.

    Returns documents from S3 if enabled, otherwise from local storage.
    """
    if is_s3_enabled():
        documents = list_user_s3_documents(user_id)
        return {"documents": documents, "storage": "s3"}
    else:
        from eval_mcp.core.user_storage import list_user_document_paths
        paths = list_user_document_paths(user_id)
        documents = [{"path": p} for p in paths]
        return {"documents": documents, "storage": "disk"}


@app.get("/api/auth/user")
async def get_current_user(request: Request):
    """Get the current authenticated user from oauth2-proxy headers.

    Returns logout URL for proper Cognito sign-out.
    """
    user_id = request.headers.get("X-Forwarded-User")
    email = request.headers.get("X-Forwarded-Email")

    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user = {
        "id": user_id,
        "email": email,
        "name": email.split("@")[0] if email else user_id,
    }

    # Build logout URL that clears both oauth2-proxy and Cognito sessions
    cognito_domain = os.environ.get("COGNITO_DOMAIN")
    client_id = os.environ.get("COGNITO_CLIENT_ID") or os.environ.get("OIDC_CLIENT_ID")
    app_url = os.environ.get("APP_URL", "")

    if cognito_domain and client_id:
        import urllib.parse
        # Reversed logout chain: browser goes to Cognito logout first (clears Cognito
        # session), then Cognito redirects to oauth2-proxy sign_out (clears proxy cookie).
        # No oauth2-proxy whitelist needed — browser navigates directly to Cognito.
        oauth2_sign_out = app_url.rstrip("/") + "/oauth2/sign_out" if app_url else "/oauth2/sign_out"
        logout_url = f"https://{cognito_domain}/logout?client_id={client_id}&logout_uri={urllib.parse.quote(oauth2_sign_out, safe='')}"
    else:
        logout_url = "/oauth2/sign_out?rd=/"

    return {"user": user, "logoutUrl": logout_url}


# ============== Data Library (datasets + judges) ==============


class DatasetPatch(BaseModel):
    name: Optional[str] = None
    tests: Optional[List[Dict]] = None


@app.get("/api/datasets")
async def list_datasets(
    search: str = "",
    user_id: str = Depends(get_current_user_id),
):
    """List the authenticated user's datasets without test payloads (cheap)."""
    from eval_mcp.core.user_storage import list_datasets_from_db

    try:
        entries = list_datasets_from_db(user_id, search)
        # Strip the heavy `tests` field — the list view only needs metadata.
        summaries = [
            {k: v for k, v in e.items() if k != "tests"}
            for e in entries
        ]
        return {"datasets": summaries}
    except Exception:
        _, safe_msg = _user_safe_error("list_datasets endpoint")
        raise HTTPException(status_code=500, detail=safe_msg)


@app.get("/api/datasets/{dataset_id}")
async def get_dataset_detail(
    dataset_id: str,
    offset: int = 0,
    limit: int = 50,
    user_id: str = Depends(get_current_user_id),
):
    """Get a dataset with a windowed slice of its tests."""
    from eval_mcp.core.user_storage import get_dataset_from_db

    try:
        data = get_dataset_from_db(user_id, dataset_id)
        if not data:
            raise HTTPException(status_code=404, detail="Dataset not found")

        tests = data.get("tests", [])
        total = len(tests)
        if limit <= 0 or limit > 500:
            limit = 50
        if offset < 0:
            offset = 0
        window = tests[offset : offset + limit]

        return {
            "id": data["id"],
            "name": data.get("name", ""),
            "source": data.get("source", {"kind": "imported"}),
            "created_at": data["created_at"],
            "updated_at": data.get("updated_at"),
            "total": total,
            "offset": offset,
            "limit": limit,
            "tests": window,
        }
    except HTTPException:
        raise
    except Exception:
        _, safe_msg = _user_safe_error("get_dataset_detail endpoint")
        raise HTTPException(status_code=500, detail=safe_msg)


@app.delete("/api/datasets/{dataset_id}")
async def delete_dataset(
    dataset_id: str,
    user_id: str = Depends(get_current_user_id),
):
    from eval_mcp.core.user_storage import delete_dataset_from_db

    try:
        ok = delete_dataset_from_db(user_id, dataset_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Dataset not found")
        return {"deleted": True}
    except HTTPException:
        raise
    except Exception:
        _, safe_msg = _user_safe_error("delete_dataset endpoint")
        raise HTTPException(status_code=500, detail=safe_msg)


@app.patch("/api/datasets/{dataset_id}")
async def patch_dataset(
    dataset_id: str,
    patch: DatasetPatch,
    user_id: str = Depends(get_current_user_id),
):
    from eval_mcp.core.user_storage import update_dataset_in_db

    try:
        updated = update_dataset_in_db(
            user_id,
            dataset_id,
            name=patch.name,
            tests=patch.tests,
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
    except HTTPException:
        raise
    except Exception:
        _, safe_msg = _user_safe_error("patch_dataset endpoint")
        raise HTTPException(status_code=500, detail=safe_msg)


@app.get("/api/datasets/{dataset_id}/export")
async def export_dataset_csv(
    dataset_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Stream a CSV with question/golden_answer + any extra vars."""
    import csv
    import io
    from eval_mcp.core.user_storage import get_dataset_from_db

    data = get_dataset_from_db(user_id, dataset_id)
    if not data:
        raise HTTPException(status_code=404, detail="Dataset not found")

    tests = data.get("tests", [])
    # Discover the union of var keys across tests so extra fields (tags,
    # expected_tools, etc) round-trip even for non-uniform datasets.
    keys: list[str] = []
    seen: set[str] = set()
    for t in tests:
        for k in (t.get("vars") or {}).keys():
            if k not in seen:
                seen.add(k)
                keys.append(k)
    # Ensure the canonical columns lead, in a stable order.
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
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}.csv"',
        },
    )


@app.get("/api/judges")
async def list_judges(
    search: str = "",
    user_id: str = Depends(get_current_user_id),
):
    from eval_mcp.core.user_storage import list_judges_from_db

    try:
        entries = list_judges_from_db(user_id, search)
        # Shape the list view to what the UI needs (omit full config body).
        summaries = []
        for e in entries:
            cfg = e.get("config") or {}
            summaries.append({
                "id": e["id"],
                "name": e.get("name", ""),
                "domain": cfg.get("domain", "general"),
                "criteria": [c.get("name") for c in cfg.get("criteria", [])],
                "created_at": e.get("created_at"),
            })
        return {"judges": summaries}
    except Exception:
        _, safe_msg = _user_safe_error("list_judges endpoint")
        raise HTTPException(status_code=500, detail=safe_msg)


@app.get("/api/judges/{judge_id}")
async def get_judge_detail(
    judge_id: str,
    user_id: str = Depends(get_current_user_id),
):
    from eval_mcp.core.user_storage import get_judge_from_db

    try:
        data = get_judge_from_db(user_id, judge_id)
        if not data:
            raise HTTPException(status_code=404, detail="Judge not found")
        return data
    except HTTPException:
        raise
    except Exception:
        _, safe_msg = _user_safe_error("get_judge_detail endpoint")
        raise HTTPException(status_code=500, detail=safe_msg)


@app.delete("/api/judges/{judge_id}")
async def delete_judge(
    judge_id: str,
    user_id: str = Depends(get_current_user_id),
):
    from eval_mcp.core.user_storage import delete_judge_from_db

    try:
        ok = delete_judge_from_db(user_id, judge_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Judge not found")
        return {"deleted": True}
    except HTTPException:
        raise
    except Exception:
        _, safe_msg = _user_safe_error("delete_judge endpoint")
        raise HTTPException(status_code=500, detail=safe_msg)


# ============== Inspect AI Viewer ==============

from backend.api.compare import router as compare_router
from backend.api.optimizations import router as optimizations_router
from backend.core.inspect_viewer import create_viewer_app, get_viewer_dist_directory
from inspect_ai._view.fastapi_server import _InspectStaticFiles
from inspect_ai._util.file import filesystem
from inspect_ai._view._dist import resolve_dist_directory

# Mount comparison API before the Inspect viewer (include_router takes priority over mount)
app.include_router(compare_router, prefix="/api/compare")
app.include_router(optimizations_router, prefix="/api/optimizations")

_log_dir = os.environ.get("INSPECT_LOG_DIR", os.environ.get("USER_STORAGE_BASE", "backend/users"))
_fs = filesystem(_log_dir)
if not _fs.exists(_log_dir):
    _fs.mkdir(_log_dir, True)
_resolved_log_dir = _fs.info(_log_dir).name

# The Inspect SPA hardcodes API calls to /api/* at root. Mount viewer API
# at /api alongside our backend routes (no conflicts: viewer uses /api/logs,
# /api/log-dir etc; our backend uses /api/chat, /api/auth, /api/sessions).
_viewer_api = create_viewer_app(log_dir=_resolved_log_dir)
app.mount("/api", _viewer_api)

# Serve the Inspect SPA static files at /inspect/
_dist_dir = resolve_dist_directory()
app.mount("/inspect", _InspectStaticFiles(directory=_dist_dir.as_posix(), html=True), name="inspect-viewer")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("BACKEND_PORT", "8080"))
    print(f"Starting backend API on port {port}")

    # Configure uvicorn logging
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        access_log=True,
        log_config={
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default"
                }
            },
            "root": {
                "level": "INFO",
                "handlers": ["console"]
            }
        }
    )
