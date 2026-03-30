"""FastAPI backend for the chat frontend."""

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.core.agent import Agent
from backend.core.bedrock_client import BedrockClient
from backend.core.database import Database
from backend.core.mcp_client import MultiMCPClient
from backend.core.s3_client import (
    is_s3_enabled,
    generate_presigned_upload_url,
    list_user_s3_documents,
    get_s3_document_content,
)
from backend.core.user_storage import save_document
from backend.core.viewer_manager import ViewerManager

# Configure logging at module level (runs when uvicorn loads the app)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [%(name)s] %(message)s',
    handlers=[logging.StreamHandler()]
)

# Create logger
logger = logging.getLogger(__name__)

# Global clients (initialized on startup)
mcp_client: Optional[MultiMCPClient] = None
bedrock_client: Optional[BedrockClient] = None
db: Optional[Database] = None
viewer_manager: Optional[ViewerManager] = None

# Cache for viewer API responses (serves stale data when DB is locked during evals)
# Key: (user_id, path), Value: (content, content_type, status_code)
viewer_api_cache: Dict[tuple, tuple] = {}


class ViewerCircuitBreaker:
    """Circuit breaker pattern for viewer requests.

    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Failing, reject requests immediately
    - HALF_OPEN: Testing if viewer recovered
    """

    def __init__(
        self,
        failure_threshold: int = 3,      # Failures before opening circuit
        recovery_timeout: float = 10.0,   # Seconds before trying again
        max_concurrent_per_user: int = 10, # Max concurrent requests per user
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.max_concurrent_per_user = max_concurrent_per_user

        # Per-user state
        self._failures: Dict[str, int] = {}           # user_id -> failure count
        self._last_failure: Dict[str, float] = {}     # user_id -> timestamp
        self._user_active: Dict[str, int] = {}        # user_id -> active request count
        self._lock = asyncio.Lock()                   # Protects _user_active

    def is_open(self, user_id: str) -> bool:
        """Check if circuit is open (should reject requests)."""
        failures = self._failures.get(user_id, 0)
        if failures < self.failure_threshold:
            return False

        # Check if recovery timeout has passed
        last_failure = self._last_failure.get(user_id, 0)
        if time.time() - last_failure > self.recovery_timeout:
            # Half-open: allow one request through to test
            return False

        return True

    def record_success(self, user_id: str):
        """Record a successful request, reset failure count."""
        self._failures[user_id] = 0

    def record_failure(self, user_id: str):
        """Record a failed request."""
        self._failures[user_id] = self._failures.get(user_id, 0) + 1
        self._last_failure[user_id] = time.time()

        if self._failures[user_id] >= self.failure_threshold:
            logger.warning(f"Circuit breaker OPEN for user {user_id} after {self._failures[user_id]} failures")

    async def try_acquire(self, user_id: str) -> bool:
        """Try to acquire permission. Returns False immediately if at limit (no waiting)."""
        if self.is_open(user_id):
            logger.warning(f"Circuit breaker rejecting request for user {user_id} (circuit open)")
            return False

        async with self._lock:
            # Check per-user limit (atomic with increment)
            user_active = self._user_active.get(user_id, 0)
            if user_active >= self.max_concurrent_per_user:
                logger.warning(f"Per-user rate limit hit for {user_id} ({user_active}/{self.max_concurrent_per_user})")
                return False

            # Acquire slot
            self._user_active[user_id] = user_active + 1
            return True

    async def release(self, user_id: str):
        """Release slot after request completes."""
        async with self._lock:
            if user_id in self._user_active:
                self._user_active[user_id] = max(0, self._user_active[user_id] - 1)


# Global circuit breaker for viewer requests
viewer_circuit_breaker = ViewerCircuitBreaker()

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
    global mcp_client, bedrock_client, db, viewer_manager

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

        # Initialize viewer manager for per-user promptfoo viewers
        viewer_manager = ViewerManager()
        await viewer_manager.start()
        print("  ✓ Viewer manager initialized")

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

        # Wait for running promptfoo evaluations to complete
        try:
            from backend.mcp_servers.synthetic.tools.run_evaluation import (
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

        if viewer_manager:
            try:
                await viewer_manager.stop()
                print("  ✓ Viewer manager stopped")
            except Exception as e:
                print(f"  ⚠ Error stopping viewer manager: {e}")
                logger.error(f"[SHUTDOWN] Viewer manager stop error: {e}")

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
    title="Promptfoo Chat Backend",
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
    from backend.core.user_storage import save_dataset_to_db
    from backend.mcp_servers.dataset.tools.save_dataset import parse_content_to_rows, rows_to_promptfoo_yaml, generate_dataset_name

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
                        dataset_id = save_dataset_to_db(user_id, dataset_name, test_cases)

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
        test_cases = rows_to_promptfoo_yaml(rows, column_mapping["question"], column_mapping["golden_answer"])

        if not test_cases:
            return {
                "success": False,
                "message": f"[Dataset save failed: No valid rows found with both question and answer]",
                "error": "No valid rows found",
            }

        dataset_name = generate_dataset_name(Path(filename).stem)
        dataset_id = save_dataset_to_db(user_id, dataset_name, test_cases)

        return {
            "success": True,
            "message": f"[Dataset uploaded successfully: '{filename}' saved as '{dataset_name}' with {len(test_cases)} QA pairs]",
            "dataset": dataset_name,
            "dataset_id": dataset_id,
            "rows_saved": len(test_cases),
        }

    except Exception as e:
        logger.error(f"Dataset processing error: {e}", exc_info=True)
        return {
            "success": False,
            "message": f"[Dataset processing failed: {str(e)}]",
            "error": str(e),
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


@app.post("/api/chat/cancel/{session_id}")
async def cancel_chat(session_id: str, user_id: str = Depends(get_current_user_id)):
    """Cancel an ongoing chat request and any running evaluation."""
    global cancelled_sessions, active_tasks

    # Check if there's an active task for this session
    if session_id not in active_tasks:
        return {"success": False, "message": "No active request to cancel"}

    task = active_tasks[session_id]
    if task.done():
        return {"success": False, "message": "Request already completed"}

    # Read eval info BEFORE cancelling (read-only, doesn't kill subprocess)
    eval_info = {}
    try:
        mcp_url = os.environ["SYNTHETIC_EVAL_MCP_URL"]
        base_url = mcp_url.replace("/mcp", "")
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{base_url}/eval-info/{user_id}", timeout=2.0)
            eval_info = resp.json()
            logger.info(f"[CANCEL] Eval info: {eval_info}")
    except Exception as e:
        logger.warning(f"[CANCEL] Failed to get eval info: {e}")

    # Store eval info so the CancelledError handler can include it in the cancel message
    cancelled_sessions[session_id] = eval_info

    # Cancel the asyncio task immediately - triggers CancelledError handler
    task.cancel()

    # Kill the evaluation subprocess and reconnect MCP
    try:
        mcp_url = os.environ["SYNTHETIC_EVAL_MCP_URL"]
        base_url = mcp_url.replace("/mcp", "")
        async with httpx.AsyncClient() as client:
            await client.post(f"{base_url}/cancel/{user_id}", timeout=5.0)
        await mcp_client.reconnect_server("synthetic-eval")
    except Exception as e:
        logger.warning(f"[CANCEL] Failed to cancel evaluation: {e}")

    logger.info(f"[CANCEL] User {user_id} cancelled session {session_id}")

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

        logger.info(f"[AGENT START] Starting agent loop for session {session_id}")

        async for event in agent.run_conversation_turn_streaming(final_message):
            # Check for cancellation
            if session_id in cancelled_sessions:
                cancel_info = cancelled_sessions[session_id]
                logger.info(f"[AGENT CANCELLED] Session {session_id} cancelled by user, eval info: {cancel_info}")
                was_cancelled = True
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

        # Save assistant message to DB (even partial if cancelled)
        if full_response:
            if was_cancelled:
                full_response += "\n\n*[Response cancelled by user]*"
                # Store cancel info on agent for _fix_orphaned_tool_uses
                cancel_info = cancelled_sessions.get(session_id, {})
                eval_id = cancel_info.get("evalId")
                if eval_id:
                    agent = session_agents.get(session_id)
                    if agent:
                        agent.cancel_info = cancel_info
            assistant_msg_id = str(uuid.uuid4())
            await db.save_message(assistant_msg_id, session_id, "assistant", full_response)
            logger.info(f"[DB SAVE] Saved assistant response for session {session_id}")

            # Update session title if this is the first message
            messages = await db.get_session_messages(session_id)
            if len(messages) == 2:
                title = user_message_for_db[:50] + "..." if len(user_message_for_db) > 50 else user_message_for_db
                await db.update_session_title(session_id, title)

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

        # Save partial response to DB
        if full_response:
            full_response += "\n\n*[Response cancelled by user]*"
            assistant_msg_id = str(uuid.uuid4())
            await db.save_message(assistant_msg_id, session_id, "assistant", full_response)
            logger.info(f"[DB SAVE] Saved partial response for cancelled session {session_id}")

    except Exception as e:
        import traceback
        logger.error(f"[BACKGROUND ERROR] Error in agent background task for session {session_id}: {e}")
        logger.error(f"[BACKGROUND ERROR] Traceback: {traceback.format_exc()}")
        await queue.put({"type": "error", "data": {"error": str(e)}})

    finally:
        # Signal completion to queue readers
        await queue.put(None)

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
        except Exception as e:
            logger.error(f"File processing failed: {e}")
            file_result_message = f"[File upload failed: {str(e)}]"
            yield f"event: progress\ndata: {json.dumps({'message': f'File processing failed: {e}'})}\n\n"

    # Build final message for agent
    if file_result_message:
        final_message = f"{request.message}\n\n{file_result_message}" if request.message else file_result_message
    else:
        final_message = request.message

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

    except Exception as e:
        import traceback
        logger.error(f"[STREAM ERROR] Error streaming to client for session {session_id}: {e}")
        logger.error(f"[STREAM ERROR] Traceback: {traceback.format_exc()}")
        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"


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
    except Exception as e:
        import traceback
        print(f"Error in chat endpoint: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
            except Exception as e:
                logger.error(f"Failed to process {ext.upper()} {filename} from S3: {e}")
                csv_results.append({
                    "filename": filename,
                    "success": False,
                    "message": f"[Failed to process {ext.upper()}: {str(e)}]",
                    "error": str(e),
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
        from backend.core.user_storage import list_user_document_paths
        paths = list_user_document_paths(user_id)
        documents = [{"path": p} for p in paths]
        return {"documents": documents, "storage": "disk"}


@app.get("/api/auth/user")
async def get_current_user(request: Request):
    """Get the current authenticated user from oauth2-proxy headers.

    Also pre-warms the viewer in the background (non-blocking).
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

    # Pre-warm viewer in background (non-blocking)
    # This also refreshes the 48-hour login timer if viewer exists
    if viewer_manager:
        await viewer_manager.on_user_login(user_id)

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


# ============== Viewer Management Endpoints ==============

class ViewerResponse(BaseModel):
    url: str
    status: str


@app.get("/api/viewer/url")
async def get_viewer_url(user_id: str = Depends(get_current_user_id)) -> ViewerResponse:
    """Get the promptfoo viewer URL for the authenticated user.

    Starts a new viewer instance if one isn't running for this user.
    Each user gets their own isolated viewer pointing to their data.
    """
    if not viewer_manager:
        raise HTTPException(status_code=500, detail="Viewer manager not initialized")

    try:
        logger.info(f"Getting viewer URL for user {user_id}")
        url = await viewer_manager.get_viewer_url(user_id)
        return ViewerResponse(url=url, status="ready")
    except RuntimeError as e:
        logger.error(f"RuntimeError getting viewer URL: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting viewer URL for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to start viewer")


@app.get("/api/viewer/status")
async def get_viewer_status(user_id: str = Depends(get_current_user_id)):
    """Get the status of the authenticated user's viewer instance."""
    if not viewer_manager:
        raise HTTPException(status_code=500, detail="Viewer manager not initialized")

    status = viewer_manager.get_viewer_status(user_id)
    if status is None:
        return {"running": False, "message": "No viewer running for this user"}

    return status


@app.post("/api/internal/invalidate-cache/{user_id}")
async def invalidate_viewer_cache(user_id: str, request: Request):
    """Invalidate cached viewer API responses for a user.

    Called by MCP server after eval completes to ensure fresh data is served.
    Only accepts requests from within the cluster (no CloudFront headers).
    """
    # Block external requests - CloudFront adds these headers
    if request.headers.get("x-amz-cf-id") or request.headers.get("cloudfront-viewer-country"):
        raise HTTPException(status_code=403, detail="Internal endpoint - not accessible externally")
    # Clear all cached responses for this user
    keys_to_remove = [key for key in viewer_api_cache if key[0] == user_id]
    for key in keys_to_remove:
        del viewer_api_cache[key]

    logger.info(f"Invalidated {len(keys_to_remove)} cached responses for user {user_id}")
    return {"success": True, "invalidated": len(keys_to_remove)}


async def validate_session(request: Request, expected_user_id: str) -> bool:
    """Validate that request is from expected user via oauth2-proxy headers."""
    user_id = request.headers.get("X-Forwarded-User")
    if not user_id:
        return False
    return user_id == expected_user_id


@app.api_route("/viewer/{user_id}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def proxy_viewer(user_id: str, path: str, request: Request):
    """Proxy requests to user's viewer instance.

    This ensures users can only access their own viewer and
    internal ports aren't exposed externally.

    Uses circuit breaker + rate limiting to prevent cascade failures:
    - Circuit breaker: After 3 failures, reject requests for 10s
    - Rate limiting: Max 10 concurrent requests per user
    """
    if not viewer_manager:
        raise HTTPException(status_code=500, detail="Viewer manager not initialized")

    # Only rate limit API calls, not static assets (page load needs many assets in parallel)
    is_api_call = path.startswith('api/')
    if is_api_call:
        # Rate limiting FIRST - reject fast before any HTTP calls
        if not await viewer_circuit_breaker.try_acquire(user_id):
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please slow down."
            )

    # Validate session - user can only access their own viewer
    if not await validate_session(request, user_id):
        if is_api_call:
            await viewer_circuit_breaker.release(user_id)  # Release slot on auth failure
        raise HTTPException(status_code=403, detail="Access denied - invalid session or user mismatch")

    try:
        # Get or start the user's viewer
        try:
            viewer_url = await viewer_manager.get_viewer_url(user_id)
        except Exception as e:
            logger.error(f"Failed to get viewer for user {user_id}: {e}")
            viewer_circuit_breaker.record_failure(user_id)
            raise HTTPException(status_code=500, detail="Failed to start viewer")

        # Build target URL
        # For SPA routes (non-asset paths), serve root and let client-side router handle it
        is_asset = any(path.endswith(ext) for ext in ['.js', '.css', '.json', '.ico', '.png', '.svg', '.woff', '.woff2', '.map']) or path.startswith('api/')
        if is_asset:
            target_url = f"{viewer_url}/{path}"
        else:
            target_url = f"{viewer_url}/"
        if request.query_params:
            target_url += f"?{request.query_params}"

        # Proxy the request
        # Don't request compressed content - let httpx handle decompression
        proxy_headers = {k: v for k, v in request.headers.items()
                         if k.lower() not in ['host', 'content-length', 'accept-encoding']}

        # Retry logic for API requests that may fail due to SQLite locking
        is_api_request = path.startswith('api/')
        max_retries = 3 if is_api_request else 1
        retry_delay = 0.5  # seconds between retries
        request_body = await request.body() if request.method in ['POST', 'PUT'] else None
        cache_key = (user_id, path)

        async with httpx.AsyncClient() as client:
            response = None
            last_error = None

            for attempt in range(max_retries):
                try:
                    # Forward the request
                    response = await client.request(
                        method=request.method,
                        url=target_url,
                        headers=proxy_headers,
                        content=request_body,
                        timeout=10.0,  # Reduced from 30s - fail fast if viewer hung
                    )

                    # Retry on 500 (likely DB locked)
                    if is_api_request and response.status_code >= 500 and attempt < max_retries - 1:
                        logger.warning(f"Viewer returned {response.status_code} for {path}, retrying ({attempt + 1}/{max_retries})")
                        await asyncio.sleep(retry_delay)
                        continue

                    break  # Success or non-retryable error
                except httpx.RequestError as e:
                    last_error = e
                    if attempt < max_retries - 1:
                        logger.warning(f"Viewer request failed, retrying ({attempt + 1}/{max_retries}): {e}")
                        await asyncio.sleep(retry_delay)
                        continue
                    # Don't raise yet - try cache first
                    response = None
                    break

            # If API request failed after retries, try to serve from cache
            if is_api_request and (response is None or response.status_code >= 500):
                if cache_key in viewer_api_cache:
                    cached_content, cached_content_type, cached_status = viewer_api_cache[cache_key]
                    logger.info(f"Serving cached response for {user_id}/{path} (DB likely locked)")
                    return Response(
                        content=cached_content,
                        status_code=cached_status,
                        media_type=cached_content_type,
                    )
                elif response is None:
                    # No cache and request failed completely
                    raise last_error or HTTPException(status_code=502, detail="Viewer unavailable")

            # Build response headers, excluding hop-by-hop headers
            response_headers = {k: v for k, v in response.headers.items()
                               if k.lower() not in ['content-encoding', 'transfer-encoding', 'content-length', 'connection']}

            content = response.content
            content_type = response.headers.get('content-type', '')

            # Cache successful API responses for use when DB is locked
            if is_api_request and response.status_code == 200:
                viewer_api_cache[cache_key] = (content, content_type, response.status_code)

            # Rewrite asset paths in HTML to use the proxy path
            if 'text/html' in content_type:
                html = content.decode('utf-8')
                base_path = f"/viewer/{user_id}"

                # JSON encode for safe JavaScript injection (prevents XSS)
                safe_base_path = json.dumps(base_path)

                # Inject script to handle SPA routing through proxy
                # 1. Strip base path so React Router sees correct route (e.g., /evals not /viewer/anna/evals)
                # 2. Restore full URL after React init (before paint) so refresh works
                # 3. Patch history/fetch/XHR to add base path to future navigations
                patch_script = f'''<script>
(function() {{
var bp = {safe_base_path};
var path = window.location.pathname;
var search = window.location.search;
var hash = window.location.hash;
var origPush = history.pushState.bind(history);
var origReplace = history.replaceState.bind(history);

// Set apiBaseUrl in localStorage for Zustand persist store
// This tells the promptfoo UI to prefix all API calls with the viewer base path
// Must run before React/Zustand hydrates to avoid race conditions
try {{
  var storageKey = 'api-config-storage';
  var stored = localStorage.getItem(storageKey);
  var state = stored ? JSON.parse(stored) : {{}};
  state.state = state.state || {{}};
  state.state.apiBaseUrl = bp;
  localStorage.setItem(storageKey, JSON.stringify(state));
  console.log('[VIEWER PROXY] Set apiBaseUrl to: ' + bp);
}} catch (e) {{
  console.warn('[VIEWER PROXY] Failed to set apiBaseUrl in localStorage:', e);
}}

// Strip base path so React Router sees /evals instead of /viewer/anna/evals
console.log('[VIEWER PROXY] path=' + path + ', bp=' + bp);
if (path.startsWith(bp)) {{
  var stripped = path.substring(bp.length) || '/';
  console.log('[VIEWER PROXY] stripping to: ' + stripped);
  origReplace(history.state, '', stripped + search + hash);
  // Restore full URL after page fully loads (after React module scripts execute)
  window.addEventListener('load', function() {{
    console.log('[VIEWER PROXY] restoring to: ' + path);
    origReplace(history.state, '', path + search + hash);
  }});
}} else {{
  console.log('[VIEWER PROXY] path does not start with bp, no stripping');
}}

// Patch history methods to add base path to future navigations
history.pushState = function(s, t, u) {{
  if (typeof u === 'string' && u.startsWith('/') && !u.startsWith(bp)) u = bp + u;
  return origPush(s, t, u);
}};
history.replaceState = function(s, t, u) {{
  if (typeof u === 'string' && u.startsWith('/') && !u.startsWith(bp)) u = bp + u;
  return origReplace(s, t, u);
}};

// Handle back/forward navigation - strip base path so React Router can match
window.addEventListener('popstate', function() {{
  var p = window.location.pathname;
  if (p.startsWith(bp)) {{
    var stripped = p.substring(bp.length) || '/';
    console.log('[VIEWER PROXY] popstate: stripping ' + p + ' to ' + stripped);
    origReplace(history.state, '', stripped + window.location.search + window.location.hash);
    // Restore after React Router processes the navigation
    setTimeout(function() {{
      origReplace(history.state, '', p + window.location.search + window.location.hash);
    }}, 50);
  }}
}});

// Patch fetch and XHR to add base path to API calls (fallback for any direct fetch usage)
var origFetch = window.fetch;
window.fetch = function(u, o) {{
  if (typeof u === 'string' && u.startsWith('/') && !u.startsWith(bp)) u = bp + u;
  return origFetch.call(this, u, o);
}};
var origOpen = XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open = function(m, u) {{
  if (typeof u === 'string' && u.startsWith('/') && !u.startsWith(bp)) u = bp + u;
  return origOpen.apply(this, arguments);
}};
}})();
</script>'''

                # Inject at start of <head>
                if '<head>' in html:
                    html = html.replace('<head>', f'<head>{patch_script}')
                elif '<HEAD>' in html:
                    html = html.replace('<HEAD>', f'<HEAD>{patch_script}')

                # Also rewrite static href/src attributes
                html = html.replace('href="/', f'href="{base_path}/')
                html = html.replace('src="/', f'src="{base_path}/')
                html = html.replace("href='/", f"href='{base_path}/")
                html = html.replace("src='/", f"src='{base_path}/")
                content = html.encode('utf-8')

            # Success - reset circuit breaker
            viewer_circuit_breaker.record_success(user_id)

            # Return proxied response
            return Response(
                content=content,
                status_code=response.status_code,
                headers=response_headers,
                media_type=content_type,
            )
    except httpx.RequestError as e:
        logger.error(f"Proxy error for user {user_id}: {e}")
        if is_api_call:
            viewer_circuit_breaker.record_failure(user_id)
        raise HTTPException(status_code=502, detail="Viewer unavailable")
    finally:
        # Release slot only for API calls
        if is_api_call:
            await viewer_circuit_breaker.release(user_id)


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
