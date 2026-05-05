"""Run Inspect AI evaluation via CLI subprocess."""

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import boto3
from botocore.config import Config
from inspect_ai.log import read_eval_log_async
from mcp.types import TextContent

from backend.core.user_storage import get_user_dir, get_user_log_dir

logger = logging.getLogger(__name__)


import re

# Pattern for valid config names: alphanumeric, underscore, dash only
_VALID_CONFIG_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')

# Pattern for valid eval IDs: alphanumeric, underscore, dash, colon only
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


async def _validate_providers(providers: List[str]) -> Dict[str, Any]:
    """Validate all Bedrock providers can be invoked.

    Tests each Bedrock provider with a minimal request.

    Returns:
        Dict with 'valid': True if all pass, or 'valid': False with 'failed_providers' list
    """
    if not providers:
        return {"valid": True, "providers": []}

    # Filter to only bedrock providers
    bedrock_models = [m for m in providers if m.startswith("bedrock/")]
    if not bedrock_models:
        return {"valid": True, "providers": providers, "note": "No Bedrock providers to validate"}

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
        # Strip bedrock/ prefix to get actual model ID
        actual_model_id = model_id.replace("bedrock/", "", 1)

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

    return {"cancelled": True, "evalId": eval_id, "configName": config_name}


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
    """Run an Inspect AI evaluation.

    Executes `inspect eval` via subprocess with the user's directory
    set for data isolation. Supports cancellation via asyncio task cancellation.

    Args:
        args: Tool arguments including:
            - configName: Name of the evaluation config (created by create_eval_config)
            - user_id: User ID for data isolation
            - maxConcurrency: Optional max concurrent requests (default: 4)

    Returns:
        MCP TextContent response with evaluation results
    """
    process: Optional[asyncio.subprocess.Process] = None
    eval_id = f"eval_{int(time.time() * 1000)}"

    try:
        config_name = args.get("configName")
        user_id = args.get("user_id")
        max_concurrency = args.get("maxConcurrency", 16)

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

        # Get user's directory for data isolation
        user_dir = get_user_dir(user_id)
        os.makedirs(user_dir, exist_ok=True)

        # Construct task file path from validated name
        task_file = str(user_dir / "configs" / f"{config_name}.py")

        # Verify task file exists
        if not Path(task_file).exists():
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"success": False, "error": f"Config '{config_name}' not found"}),
                )
            ]

        # Read the task file to extract provider list for validation
        try:
            task_content = Path(task_file).read_text()
            # Extract providers from the task file for validation
            providers = []
            for line in task_content.split("\n"):
                if "bedrock/" in line and '"' in line:
                    # Extract model IDs from lines like: "bedrock/us.anthropic.claude-..."
                    import re as _re
                    matches = _re.findall(r'"(bedrock/[^"]+)"', line)
                    providers.extend(matches)

            if providers:
                validation = await _validate_providers(providers)
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
        except Exception as e:
            logger.warning(f"Could not validate providers: {e}")

        # Log directory — S3 in production, local in dev
        log_dir_str = get_user_log_dir(user_id)

        # For local filesystem, ensure the directory exists
        if not log_dir_str.startswith("s3://"):
            Path(log_dir_str).mkdir(parents=True, exist_ok=True)

        # Set up environment
        env = os.environ.copy()
        env["INSPECT_LOG_DIR"] = log_dir_str
        # Ensure AWS region is set for Bedrock
        region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
        env["AWS_REGION"] = region
        env["AWS_DEFAULT_REGION"] = region

        # Build inspect eval command with relative path (Inspect requires non-absolute paths)
        relative_task = f"configs/{config_name}.py"

        # Extract model providers from the JSON config file
        models = []
        config_json_path = user_dir / "configs" / f"{config_name}.json"
        if config_json_path.exists():
            config_data = json.loads(config_json_path.read_text())
            # Agent evals use single "model" field; standard evals use "providers" list
            if config_data.get("model"):
                models = [config_data["model"]]
            else:
                models = config_data.get("providers", [])
        else:
            # Fallback: scan task file for provider strings
            for line in task_content.split("\n"):
                if '"bedrock/' in line or '"openai/' in line or '"anthropic/' in line or '"google/' in line:
                    import re as _re
                    matches = _re.findall(r'"([^"]+/[^"]+)"', line)
                    models.extend(matches)

        # Agent evals cap concurrency (each sample = separate K8s pod + LLM calls)
        is_agent_eval = config_data.get("agent_image") if config_json_path.exists() else False
        effective_concurrency = min(max_concurrency, 4) if is_agent_eval else max_concurrency

        cmd: List[str] = [
            "inspect", "eval",
            relative_task,
            "--max-connections", str(effective_concurrency),
            "--no-log-images",
            "--no-fail-on-error",
            "--log-shared", "10",
        ]

        # Pass models to inspect eval (comma-separated for multiple)
        if models:
            cmd.extend(["--model", ",".join(models)])

        # Run the evaluation from the user's directory
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(user_dir),
            start_new_session=True,
        )

        logger.info(f"Started evaluation process {process.pid} for user {user_id}")

        # Register for cancellation
        _running_evaluations[user_id] = {
            "process": process,
            "eval_id": eval_id,
            "config_name": config_name,
        }

        # 24-hour timeout for evaluations
        EVAL_TIMEOUT_SECONDS = 24 * 60 * 60  # 24 hours

        try:
            await asyncio.wait_for(process.wait(), timeout=EVAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning(f"Evaluation timed out after {EVAL_TIMEOUT_SECONDS}s for user {user_id}")
            await _terminate_process_gracefully(process)
            _running_evaluations.pop(user_id, None)
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "evalId": eval_id,
                        "configName": config_name,
                        "error": "Evaluation timed out after 24 hours.",
                    }),
                )
            ]
        finally:
            _running_evaluations.pop(user_id, None)

        # Read stderr only on failure
        stderr_str = ""
        if process.returncode != 0 and process.stderr:
            try:
                stderr_bytes = await asyncio.wait_for(process.stderr.read(), timeout=5)
                stderr_str = stderr_bytes.decode("utf-8") if stderr_bytes else ""
            except (asyncio.TimeoutError, Exception):
                stderr_str = "(stderr unavailable)"

        # Retry failed/incomplete samples from this run
        try:
            from inspect_ai._view.common import list_eval_logs_async
            all_logs = await list_eval_logs_async(log_dir_str)
            # Find the run_id from this eval (all logs in a multi-model run share it)
            this_run_id = None
            for log_info in all_logs:
                log_check = await read_eval_log_async(log_info.name, header_only=True)
                if log_check.eval.run_id:
                    this_run_id = log_check.eval.run_id
                    break

            if this_run_id:
                logs_to_retry = []
                for log_info in all_logs:
                    log_check = await read_eval_log_async(log_info.name, header_only=True)
                    if log_check.eval.run_id == this_run_id and log_check.status in ("error", "cancelled", "started"):
                        logs_to_retry.append(log_info.name)

                if logs_to_retry:
                    logger.info(f"Retrying {len(logs_to_retry)} failed logs (run_id={this_run_id}) for user {user_id}")
                    retry_cmd = [
                        "inspect", "eval-retry",
                        *[str(l) for l in logs_to_retry],
                        "--max-connections", str(effective_concurrency),
                        "--no-log-images",
                        "--no-fail-on-error",
                    ]
                    retry_process = await asyncio.create_subprocess_exec(
                        *retry_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env,
                        cwd=str(user_dir),
                        start_new_session=True,
                    )
                    await asyncio.wait_for(retry_process.wait(), timeout=3600)
                    logger.info(f"Retry completed with exit code {retry_process.returncode}")
        except Exception as e:
            logger.warning(f"Retry attempt failed: {e}")

        # Pre-compute comparison JSON so the viewer reads instantly
        try:
            from backend.core.eval_results import precompute_eval_results
            await precompute_eval_results(user_id)
        except Exception as e:
            logger.warning(f"Failed to pre-compute eval results: {e}")

        if process.returncode != 0:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "evalId": eval_id,
                        "configName": config_name,
                        "error": f"Evaluation failed with exit code {process.returncode}",
                        "stderr": stderr_str[:2000],
                    }),
                )
            ]

        # Read results from the latest .eval log file
        results_summary = None
        run_id = None
        try:
            from inspect_ai._view.common import list_eval_logs_async
            eval_logs = await list_eval_logs_async(log_dir_str)
            if eval_logs:
                latest_log = eval_logs[0]
                log = await read_eval_log_async(latest_log.name, header_only=True)
                scores = []
                if log.results and log.results.scores:
                    for s in log.results.scores:
                        scores.append({"scorer": s.name, "metrics": {n: m.value for n, m in s.metrics.items()}})
                results_summary = {
                    "totalTests": log.eval.dataset.samples if log.eval.dataset else 0,
                    "scores": scores,
                    "logFile": latest_log.name,
                }
                run_id = log.eval.run_id
        except Exception as e:
            results_summary = {"error": f"Could not parse results: {str(e)}"}

        result = {
            "success": True,
            "evalId": eval_id,
            "configName": config_name,
            "runId": run_id,
            "viewerUrl": f"/results?group={run_id}" if run_id else "/results",
            "userDir": str(user_dir),
            "message": "Evaluation completed successfully",
            "summary": results_summary,
        }

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    except asyncio.CancelledError:
        logger.info(f"Evaluation cancelled for user {args.get('user_id')}")
        if process is not None:
            await _terminate_process_gracefully(process)
        _running_evaluations.pop(args.get('user_id'), None)
        return [
            TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "evalId": eval_id,
                    "configName": args.get("configName"),
                    "error": "Evaluation was cancelled.",
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


async def handle_retry_evaluation(args: Dict[str, Any]) -> List[TextContent]:
    """Retry incomplete/failed evaluations for a user.

    Finds eval logs with status 'error', 'cancelled', or 'started' (killed mid-run)
    and retries only the failed samples using inspect eval-retry.
    """
    try:
        user_id = args.get("user_id")
        max_concurrency = args.get("maxConcurrency", 16)

        if not user_id:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": "user_id is required"}))]

        user_dir = get_user_dir(user_id)
        log_dir_str = get_user_log_dir(user_id)

        from inspect_ai._view.common import list_eval_logs_async

        all_logs = await list_eval_logs_async(log_dir_str)
        logs_to_retry = []

        for log_info in all_logs:
            try:
                log_check = await read_eval_log_async(log_info.name, header_only=True)
                if log_check.status in ("error", "cancelled", "started"):
                    logs_to_retry.append(log_info.name)
            except Exception:
                continue

        if not logs_to_retry:
            return [TextContent(type="text", text=json.dumps({
                "success": True,
                "message": "No failed evaluations to retry. All evaluations completed successfully.",
                "retried": 0,
            }))]

        # Set up environment
        env = os.environ.copy()
        env["INSPECT_LOG_DIR"] = log_dir_str
        region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
        env["AWS_REGION"] = region
        env["AWS_DEFAULT_REGION"] = region

        retry_cmd = [
            "inspect", "eval-retry",
            *[str(l) for l in logs_to_retry],
            "--max-connections", str(max_concurrency),
            "--no-log-images",
            "--no-fail-on-error",
        ]

        logger.info(f"Retrying {len(logs_to_retry)} failed evals for user {user_id}")

        process = await asyncio.create_subprocess_exec(
            *retry_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(user_dir),
            start_new_session=True,
        )

        _running_evaluations[user_id] = {
            "process": process,
            "eval_id": "retry",
            "config_name": "retry",
        }

        try:
            await asyncio.wait_for(process.wait(), timeout=3600)
        except asyncio.TimeoutError:
            await _terminate_process_gracefully(process)
            _running_evaluations.pop(user_id, None)
            return [TextContent(type="text", text=json.dumps({
                "success": False,
                "error": "Retry timed out after 1 hour.",
                "retried": len(logs_to_retry),
            }))]
        finally:
            _running_evaluations.pop(user_id, None)

        # Pre-compute results
        try:
            from backend.core.eval_results import precompute_eval_results
            await precompute_eval_results(user_id)
        except Exception:
            pass

        # Read results
        results_summary = []
        updated_logs = await list_eval_logs_async(log_dir_str)
        for log_info in updated_logs[:10]:
            try:
                log = await read_eval_log_async(log_info.name, header_only=True)
                if log.results and log.results.scores:
                    results_summary.append({
                        "model": log.eval.model,
                        "status": log.status,
                        "scores": {n: m.value for s in log.results.scores for n, m in s.metrics.items()},
                    })
            except Exception:
                continue

        run_id = None
        if updated_logs:
            try:
                latest = await read_eval_log_async(updated_logs[0].name, header_only=True)
                run_id = latest.eval.run_id
            except Exception:
                pass

        return [TextContent(type="text", text=json.dumps({
            "success": True,
            "message": f"Retried {len(logs_to_retry)} evaluations",
            "retried": len(logs_to_retry),
            "viewerUrl": f"/results?group={run_id}" if run_id else "/results",
            "results": results_summary,
        }, indent=2))]

    except Exception as e:
        return [TextContent(type="text", text=json.dumps({
            "success": False,
            "error": f"Retry failed: {str(e)}",
        }))]
