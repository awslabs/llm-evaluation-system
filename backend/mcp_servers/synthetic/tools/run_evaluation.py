"""Run promptfoo evaluation via CLI subprocess."""

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import boto3
import httpx
import yaml
from botocore.config import Config
from mcp.types import TextContent

from backend.core.user_storage import get_user_promptfoo_dir

logger = logging.getLogger(__name__)


import re

# Pattern for valid config names: alphanumeric, underscore, dash only
_VALID_CONFIG_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')

# Pattern for valid promptfoo eval IDs: alphanumeric, underscore, dash, colon only
_VALID_EVAL_ID_PATTERN = re.compile(r'^[a-zA-Z0-9_:-]+$')


def _validate_config_name(config_name: str) -> str:
    """Validate that a config name is safe (no path traversal possible).

    Args:
        config_name: The config name to validate

    Returns:
        The validated config name

    Raises:
        ValueError: If config name contains invalid characters
    """
    if not config_name:
        raise ValueError("Config name cannot be empty")

    if not _VALID_CONFIG_NAME_PATTERN.match(config_name):
        raise ValueError(
            f"Invalid config name '{config_name}'. "
            f"Only alphanumeric characters, underscores, and dashes are allowed."
        )

    # Additional safety: reject any path-like patterns
    if '/' in config_name or '\\' in config_name or '..' in config_name:
        raise ValueError(f"Invalid config name '{config_name}'. Path separators not allowed.")

    return config_name


async def _validate_providers(config_path: str) -> Dict[str, Any]:
    """Validate all providers in a config file can be invoked.

    Reads the config, extracts providers, and tests each one with a minimal request.

    Returns:
        Dict with 'valid': True if all pass, or 'valid': False with 'failed_providers' list
    """
    # Read config file
    try:
        with open(config_path) as f:
            config = yaml.safe_load(f)
    except Exception as e:
        return {"valid": False, "error": f"Failed to read config: {e}"}

    providers = config.get("providers", [])
    if not providers:
        return {"valid": True, "providers": []}

    # Extract model IDs from providers (handle both string and dict formats)
    model_ids = []
    for p in providers:
        if isinstance(p, str):
            model_ids.append(p)
        elif isinstance(p, dict) and "id" in p:
            model_ids.append(p["id"])

    # Filter to only bedrock providers
    bedrock_models = [m for m in model_ids if m.startswith("bedrock:")]
    if not bedrock_models:
        return {"valid": True, "providers": model_ids, "note": "No Bedrock providers to validate"}

    # Validate each bedrock model
    failed = []
    region = os.environ.get("AWS_REGION", "us-west-2")

    config = Config(
        region_name=region,
        read_timeout=15,
        connect_timeout=10,
        retries={"max_attempts": 1},
    )
    runtime_client = boto3.client("bedrock-runtime", config=config)

    for model_id in bedrock_models:
        # Strip bedrock: or bedrock:converse: prefix to get actual model ID
        if model_id.startswith("bedrock:converse:"):
            actual_model_id = model_id[17:]  # len("bedrock:converse:") = 17
        elif model_id.startswith("bedrock:"):
            actual_model_id = model_id[8:]
        else:
            actual_model_id = model_id

        try:
            # Use Converse API - provider-agnostic
            runtime_client.converse(
                modelId=actual_model_id,
                messages=[{"role": "user", "content": [{"text": "Hi"}]}],
                inferenceConfig={"maxTokens": 10},
            )
        except Exception as e:
            error_msg = str(e)
            if "AccessDeniedException" in error_msg:
                hint = "Model not enabled in AWS account"
            elif "ValidationException" in error_msg:
                hint = "Invalid model ID"
            elif "ResourceNotFoundException" in error_msg:
                hint = "Model not found"
            else:
                hint = error_msg[:200]

            failed.append({"model": model_id, "error": hint})
            logger.warning(f"Provider validation failed for {model_id}: {hint}")

    if failed:
        return {
            "valid": False,
            "failed_providers": failed,
            "message": f"{len(failed)} of {len(bedrock_models)} providers failed validation",
        }

    return {"valid": True, "providers": bedrock_models}

# Registry of running evaluations: user_id -> (process, eval_id, config_name)
_running_evaluations: Dict[str, Dict[str, Any]] = {}


def get_running_eval_info(user_id: str) -> Dict[str, Any]:
    """Get info about a running evaluation without stopping it."""
    entry = _running_evaluations.get(user_id)
    if not entry:
        return {"running": False, "evalId": None, "configName": None}
    return {
        "running": True,
        "evalId": entry.get("eval_id"),
        "configName": entry.get("config_name"),
    }


async def cancel_user_evaluation(user_id: str) -> Dict[str, Any]:
    """Cancel a running evaluation for a user. Returns eval info so the agent can resume."""
    entry = _running_evaluations.get(user_id)
    eval_id = None
    config_name = None

    if entry:
        process = entry["process"]
        eval_id = entry.get("eval_id")
        config_name = entry.get("config_name")
        if process.returncode is None:
            await _terminate_process_gracefully(process)
        _running_evaluations.pop(user_id, None)
        logger.info(f"Cancelled evaluation {eval_id} for user {user_id}")

    # Always export partial results from SQLite on cancel
    user_dir = get_user_promptfoo_dir(user_id)
    await _export_partial_eval(user_dir, reason="cancelled")

    return {"cancelled": True, "evalId": eval_id, "configName": config_name}


async def _export_partial_eval(user_dir, reason: str = "cancelled") -> Optional[str]:
    """Export partial eval results from SQLite to JSON after cancellation/timeout.

    Returns the path to the exported file, or None if export failed.
    """
    import sqlite3
    from pathlib import Path

    db_path = Path(user_dir) / "promptfoo.db"
    if not db_path.exists():
        logger.warning(f"No SQLite database found at {db_path}")
        return None

    try:
        # Get the latest eval ID from SQLite
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM evals ORDER BY created_at DESC LIMIT 1")
        row = cursor.fetchone()
        conn.close()

        if not row:
            logger.warning("No evals found in database")
            return None

        eval_id = row[0]

        # Create results directory
        results_dir = Path(user_dir) / "results"
        results_dir.mkdir(exist_ok=True)

        # Export using promptfoo CLI
        partial_file = results_dir / f"eval_{int(time.time() * 1000)}_partial.json"
        env = os.environ.copy()
        env["PROMPTFOO_CONFIG_DIR"] = str(user_dir)

        export_process = await asyncio.create_subprocess_exec(
            "promptfoo", "export", "eval", eval_id, "-o", str(partial_file),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        await asyncio.wait_for(export_process.communicate(), timeout=30)

        if partial_file.exists():
            logger.info(f"Exported partial eval to {partial_file} (reason: {reason})")
            return str(partial_file)
        else:
            logger.warning(f"Export command completed but file not created: {partial_file}")
            return None

    except Exception as e:
        logger.warning(f"Failed to export partial eval: {e}")
        return None


async def _terminate_process_gracefully(
    process: asyncio.subprocess.Process,
    timeout: float = 5.0,
) -> None:
    """Terminate a subprocess and its children gracefully.

    Kills the entire process group to ensure child processes don't become orphans.
    """
    if process.returncode is not None:
        return  # Already terminated

    pid = process.pid

    # Kill entire process group (main process + all children)
    try:
        os.killpg(pid, signal.SIGTERM)
        logger.info(f"Sent SIGTERM to process group {pid}")
    except (ProcessLookupError, PermissionError):
        return  # Process already gone

    # Wait for graceful shutdown
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
        logger.info(f"Process group {pid} terminated gracefully")
        return
    except asyncio.TimeoutError:
        pass

    # Force kill the process group
    try:
        os.killpg(pid, signal.SIGKILL)
        logger.warning(f"Sent SIGKILL to process group {pid}")
        await process.wait()
    except (ProcessLookupError, PermissionError):
        pass


async def handle_run_evaluation(args: Dict[str, Any]) -> List[TextContent]:
    """Run a promptfoo evaluation.

    Executes `promptfoo eval` via subprocess with the user's PROMPTFOO_CONFIG_DIR
    set for data isolation. Supports cancellation via asyncio task cancellation.

    Args:
        args: Tool arguments including:
            - configName: Name of the evaluation config (created by create_eval_config)
            - user_id: User ID for data isolation
            - maxConcurrency: Optional max concurrent requests (default: 4)
            - write: Whether to write results to database (default: True)
            - resumeEvalId: Optional eval ID to resume an incomplete evaluation

    Returns:
        MCP TextContent response with evaluation results
    """
    process: Optional[asyncio.subprocess.Process] = None

    try:
        config_name = args.get("configName")
        user_id = args.get("user_id")
        max_concurrency = args.get("maxConcurrency", 4)
        write = args.get("write", True)
        resume_eval_id = args.get("resumeEvalId")

        # Validate required args
        if not user_id:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"success": False, "error": "user_id is required"}),
                )
            ]
        if not config_name:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"success": False, "error": "configName is required"}),
                )
            ]

        # Validate config name is safe (alphanumeric only - no path injection possible)
        try:
            config_name = _validate_config_name(config_name)
        except ValueError as e:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"success": False, "error": str(e)}),
                )
            ]

        # Validate maxConcurrency is a bounded integer
        try:
            max_concurrency = int(max_concurrency)
            if not 1 <= max_concurrency <= 64:
                raise ValueError("out of range")
        except (TypeError, ValueError):
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"success": False, "error": "maxConcurrency must be an integer between 1 and 64"}),
                )
            ]

        # Validate resumeEvalId format if provided
        if resume_eval_id is not None:
            resume_eval_id = str(resume_eval_id)
            if not _VALID_EVAL_ID_PATTERN.match(resume_eval_id) or len(resume_eval_id) > 128:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps({"success": False, "error": "Invalid resumeEvalId format"}),
                    )
                ]

        # Get user's promptfoo directory for data isolation
        user_dir = get_user_promptfoo_dir(user_id)
        os.makedirs(user_dir, exist_ok=True)

        # Construct config path from validated name - path is fully server-controlled
        config_path = str(user_dir / "configs" / f"{config_name}.yaml")

        # Verify config file exists
        if not Path(config_path).exists():
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"success": False, "error": f"Config '{config_name}' not found"}),
                )
            ]

        # Validate providers before running eval
        validation = await _validate_providers(config_path)
        if not validation.get("valid", False):
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "error": "Provider validation failed",
                        "validation": validation,
                        "hint": "Some models are not accessible. Check if they are enabled in your AWS account.",
                    }, indent=2),
                )
            ]

        logger.info(f"Provider validation passed for {len(validation.get('providers', []))} models")

        # Create results directory for durable storage of each eval
        results_dir = user_dir / "results"
        results_dir.mkdir(exist_ok=True)

        # Generate unique eval ID based on timestamp
        eval_id = f"eval_{int(time.time() * 1000)}"
        results_file = results_dir / f"{eval_id}.json"

        # Build promptfoo eval command
        # Note: promptfoo writes to database by default, use --no-write to disable
        # Each eval is saved to a unique JSON file for durability
        # All paths are constructed from validated inputs (config_name validated above)
        config_path_str = str(config_path)
        results_file_str = str(results_file)

        # Set up environment with user's config directory
        env = os.environ.copy()
        env["PROMPTFOO_CONFIG_DIR"] = str(user_dir)
        # Pass AWS_REGION as AWS_BEDROCK_REGION for promptfoo (it doesn't read AWS_REGION)
        if "AWS_REGION" in env and "AWS_BEDROCK_REGION" not in env:
            env["AWS_BEDROCK_REGION"] = env["AWS_REGION"]

        # Build command arguments - all values are validated above
        cmd: List[str] = [
            "promptfoo", "eval",
            "-c", config_path_str,
            "--max-concurrency", str(max_concurrency),
            "--no-progress-bar",
            "--no-cache",
            "-o", results_file_str,
        ]

        # Resume requires database persistence (--no-write is incompatible)
        if resume_eval_id:
            cmd.extend(["--resume", str(resume_eval_id)])
            logger.info(f"Resuming evaluation {resume_eval_id} for user {user_id}")
        elif not write:
            cmd.append("--no-write")

        # Run the evaluation using create_subprocess_exec (no shell interpretation).
        # All cmd elements are validated: config_path from _validate_config_name,
        # max_concurrency bounded to 1-64, resume_eval_id matched against alphanumeric pattern.
        process = await asyncio.create_subprocess_exec(  # nosemgrep: dangerous-asyncio-create-exec-audit
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )

        logger.info(f"Started evaluation process {process.pid} for user {user_id}")

        # Capture the eval ID from promptfoo's first stdout line (printed before tests run)
        promptfoo_eval_id = None
        try:
            first_line = await asyncio.wait_for(process.stdout.readline(), timeout=60)
            if first_line:
                decoded = first_line.decode("utf-8").strip()
                if decoded.startswith("EVAL_ID:"):
                    promptfoo_eval_id = decoded.split(":", 1)[1]
                    logger.info(f"Captured eval ID: {promptfoo_eval_id}")
        except asyncio.TimeoutError:
            logger.warning("Timed out waiting for eval ID from promptfoo")

        # Register for cancellation (with eval ID so cancel can return it to the agent)
        _running_evaluations[user_id] = {
            "process": process,
            "eval_id": promptfoo_eval_id,
            "config_name": config_name,
        }

        # 24-hour timeout for evaluations
        EVAL_TIMEOUT_SECONDS = 24 * 60 * 60  # 24 hours

        try:
            # Use wait() instead of communicate() — communicate() hangs when
            # child processes inherit pipe file descriptors and outlive the parent.
            # We already captured the eval ID from stdout; results go to the database.
            await asyncio.wait_for(process.wait(), timeout=EVAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning(f"Evaluation timed out after {EVAL_TIMEOUT_SECONDS}s for user {user_id}")
            await _terminate_process_gracefully(process)
            _running_evaluations.pop(user_id, None)
            partial_file = await _export_partial_eval(user_dir, reason="timeout")
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "evalId": promptfoo_eval_id,
                        "configName": config_name,
                        "error": "Evaluation timed out after 24 hours. Partial results are saved in the database. Resume with resumeEvalId to continue from where it left off.",
                        "partial_results": partial_file,
                    }),
                )
            ]
        finally:
            _running_evaluations.pop(user_id, None)

        # Read stderr only on failure (with timeout to avoid hanging on orphan pipes)
        stderr_str = ""
        if process.returncode not in (0, 100) and process.stderr:
            try:
                stderr_bytes = await asyncio.wait_for(process.stderr.read(), timeout=5)
                stderr_str = stderr_bytes.decode("utf-8") if stderr_bytes else ""
            except (asyncio.TimeoutError, Exception):
                stderr_str = "(stderr unavailable)"

        # Invalidate viewer cache so fresh results are shown
        try:
            backend_url = os.environ.get("BACKEND_URL", "http://backend:8080")
            async with httpx.AsyncClient() as client:
                await client.post(f"{backend_url}/api/internal/invalidate-cache/{user_id}", timeout=5.0)
        except Exception:
            # Don't fail the eval if cache invalidation fails
            pass

        # Exit code 0 = all tests passed, 100 = some tests failed (not an error)
        # Only treat other exit codes (like 1) as actual errors
        if process.returncode not in (0, 100):
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "evalId": promptfoo_eval_id,
                        "configName": config_name,
                        "error": f"Evaluation failed with exit code {process.returncode}",
                        "stderr": stderr_str[:2000],
                    }),
                )
            ]

        # Try to read the results file (uses results_file defined earlier)
        results_summary = None
        if results_file.exists():
            try:
                with open(results_file, encoding="utf-8") as f:
                    results_data = json.load(f)
                    # Extract summary statistics
                    results = results_data.get("results", [])
                    stats = results_data.get("stats", {})
                    results_summary = {
                        "totalTests": len(results),
                        "successes": stats.get("successes", 0),
                        "failures": stats.get("failures", 0),
                        "passRate": f"{(stats.get('successes', 0) / max(len(results), 1) * 100):.1f}%",
                        "results": results[:10],  # First 10 results for preview
                    }
            except Exception as e:
                results_summary = {"error": f"Could not parse results: {str(e)}"}

        result = {
            "success": True,
            "evalId": promptfoo_eval_id,
            "configName": config_name,
            "userDir": str(user_dir),
            "message": "Evaluation completed successfully",
            "summary": results_summary,
        }

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    except asyncio.CancelledError:
        # Handle cancellation - terminate subprocess gracefully, return eval ID so agent can resume
        logger.info(f"Evaluation cancelled for user {args.get('user_id')}")
        if process is not None:
            await _terminate_process_gracefully(process)
        _running_evaluations.pop(args.get('user_id'), None)
        return [
            TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "evalId": promptfoo_eval_id,
                    "configName": args.get("configName"),
                    "error": "Evaluation was cancelled. Partial results are saved in the database. Resume with resumeEvalId to continue from where it left off.",
                }),
            )
        ]

    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": f"Failed to run evaluation: {str(e)}",
                }),
            )
        ]
