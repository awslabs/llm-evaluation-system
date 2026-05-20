"""Run Inspect AI evaluation via CLI subprocess."""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import boto3
from botocore.config import Config
from inspect_ai.log import read_eval_log_async
from mcp.types import TextContent

from eval_mcp.core.bedrock_client import raise_if_autodetect_error
from eval_mcp.core.user_storage import get_user_dir, get_user_log_dir
from eval_mcp.tools.external_providers import _refresh_keys_from_file

logger = logging.getLogger(__name__)

# Invoke inspect-ai via the same interpreter that's running the MCP,
# guaranteeing the right environment (no PATH resolution needed).
_INSPECT_CMD = [sys.executable, "-m", "inspect_ai"]


import re

# Pattern for valid config names: alphanumeric, underscore, dash only
_VALID_CONFIG_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')

# Pattern for valid eval IDs: alphanumeric, underscore, dash, colon only
_VALID_EVAL_ID_PATTERN = re.compile(r'^[a-zA-Z0-9_:-]+$')


def is_catastrophic_eval_failure(scores: list, log_results: Any) -> bool:
    """True when the eval ran to completion but every sample errored.

    Returns False when scores were produced (a real eval, even if 0%).
    Returns True when no scores were produced AND either:
      - the log has no results object at all (the task crashed during setup), or
      - results.total_samples > 0 but results.completed_samples == 0
        (every sample raised; nothing got far enough to be scored).

    Used by handle_run_evaluation to surface success=false instead of the
    misleading success=true with scores=[] we used to return. This is the
    one signal that lets a caller distinguish 'real bad scores' from 'the
    capture pipeline silently broke and we never even ran your agent'.

    Pure function (no I/O) so it's testable without spinning up Inspect.
    """
    if scores:
        return False
    if log_results is None:
        return True
    total = getattr(log_results, "total_samples", 0) or 0
    completed = getattr(log_results, "completed_samples", 0) or 0
    return total > 0 and completed == 0


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

    # Surface the multi-profile autodetect error here rather than letting
    # boto3 fail with "Unable to locate credentials" deep in the validator.
    raise_if_autodetect_error()

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
    """Cancel a running evaluation for a user. Returns eval info so the agent can resume.

    Returns ``{"cancelled": False, "reason": "no running eval"}`` when
    nothing was registered for this user. That happens routinely on EKS:
    the cancel HTTP request can land on a different backend pod than the
    one running the eval, and the wrong-pod sidecar correctly reports it
    has nothing to kill. Surfacing that clearly in logs (vs. the
    previous unconditional ``cancelled: True``) makes the cross-pod
    case diagnosable.
    """
    entry = _running_evaluations.get(user_id)
    if not entry:
        logger.info(f"No running evaluation to cancel for user {user_id}")
        return {"cancelled": False, "reason": "no running eval", "evalId": None, "configName": None}

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

        # Read the task file (and sibling JSON config, if present) to extract
        # provider list for validation. Pipeline evals keep model IDs in the
        # JSON config and only reference them via CONFIG[...] in the .py — so
        # scanning just the .py misses them and validation silently skips.
        try:
            sources = [Path(task_file).read_text()]
            json_config = Path(task_file).with_suffix(".json")
            if json_config.exists():
                sources.append(json_config.read_text())

            import re as _re
            provider_pattern = _re.compile(r'"(bedrock/[^"]+)"')
            providers = list({m for src in sources for m in provider_pattern.findall(src)})

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
        _refresh_keys_from_file()
        env = os.environ.copy()
        env["INSPECT_LOG_DIR"] = log_dir_str
        # Ensure AWS region is set for Bedrock
        region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
        env["AWS_REGION"] = region
        env["AWS_DEFAULT_REGION"] = region

        # Cold-storage path for raw OTel records. The receiver inside each
        # solver picks this up (via EVAL_MCP_RAW_OTEL_PATH) and appends every
        # received span/log to a JSONL file alongside the eval log. If the
        # ModelEvent projection ever drops data due to a future bug, the raw
        # records are still on disk and we can re-derive offline without
        # re-running the eval. Best-effort: failures here do not block.
        if not log_dir_str.startswith("s3://"):
            raw_otel_dir = Path(log_dir_str) / "raw_otel"
            try:
                raw_otel_dir.mkdir(parents=True, exist_ok=True)
                env["EVAL_MCP_RAW_OTEL_PATH"] = str(raw_otel_dir / f"{eval_id}.jsonl")
            except OSError as e:
                logger.warning(f"Could not set up raw OTel cold storage: {e}")

        # Build inspect eval command with relative path (Inspect requires non-absolute paths)
        relative_task = f"configs/{config_name}.py"

        # Extract model providers from the JSON config file
        models = []
        config_data = None
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

        # Pre-flight capture check for agent evals: spawn the agent once
        # with a trivial prompt and verify ≥1 Bedrock span lands in our
        # OTLP receiver. Without this, a broken capture pipeline would let
        # the eval run to completion and report success=true with empty
        # scores. We only run this for agent evals — standard model evals
        # don't go through the OTLP receiver path.
        if config_data and config_data.get("agent_path"):
            try:
                from eval_mcp.canary import run_canary
                canary = await asyncio.to_thread(
                    run_canary,
                    agent_path=config_data["agent_path"],
                    agent_entry=config_data.get("agent_entry", "run_agent"),
                    requirements_path=config_data.get("requirements_path"),
                    venv_python=config_data.get("venv_python"),
                )
                if not canary.ok:
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps({
                                "success": False,
                                "evalId": eval_id,
                                "configName": config_name,
                                "error": canary.error,
                                "agentStderr": canary.agent_stderr,
                                "spansSeen": canary.spans_seen,
                                "llmSpansSeen": canary.llm_spans_seen,
                                "hint": (
                                    "The eval was aborted before running because "
                                    "we wouldn't have been able to capture "
                                    "anything. Fix the agent or the OTel install, "
                                    "then re-run."
                                ),
                            }, indent=2),
                        )
                    ]
                logger.info(
                    f"Pre-flight canary passed: {canary.llm_spans_seen} LLM "
                    f"spans captured from {canary.spans_seen} total."
                )
            except Exception as e:
                # Don't block the eval on a canary infrastructure problem —
                # log loudly and let it run. The fail-loud check will still
                # catch any actual silent failure downstream.
                logger.warning(f"Pre-flight canary skipped due to error: {e}")

        # Use Inspect's --adaptive-connections: auto-tunes parallelism based on
        # actual provider throttling. Recommended by Inspect over a fixed value,
        # and spares users from guessing a Bedrock quota they don't know.
        cmd: List[str] = [
            *_INSPECT_CMD, "eval",
            relative_task,
            "--adaptive-connections", "true",
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
                        *_INSPECT_CMD, "eval-retry",
                        *[str(l) for l in logs_to_retry],
                        "--adaptive-connections", "true",
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
            from eval_mcp.core.eval_results import precompute_eval_results
            await precompute_eval_results(user_id)
        except Exception as e:
            logger.warning(f"Failed to pre-compute eval results: {e}")

        # Sync logs to S3 if configured
        try:
            from eval_mcp.config import get_bucket
            if get_bucket():
                from eval_mcp.s3_sync import sync_logs_up
                sync_logs_up(user_id, log_dir=Path(log_dir_str))
        except Exception as e:
            logger.warning(f"S3 log sync failed: {e}")

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
        all_samples_errored = False
        first_sample_error = None
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

                # Detect silent catastrophic failure: Inspect ran to completion
                # but every sample errored, so no scores were produced. We used
                # to report success=true with an empty scores list, which hid
                # bugs like the OTel sitecustomize grandchild-leak.
                all_samples_errored = is_catastrophic_eval_failure(scores, log.results)

                # Pull the first sample error for the fail-loud response so the
                # caller sees the actual cause (missing creds, adapter crash,
                # env leak) without opening the .eval file themselves.
                if all_samples_errored:
                    try:
                        full_log = await read_eval_log_async(latest_log.name)
                        for s in (full_log.samples or []):
                            if s.error:
                                first_sample_error = str(s.error.message)[:2000]
                                break
                    except Exception:
                        pass

                # Replicate the new .eval log to S3 (no-op if bucket isn't set)
                try:
                    from eval_mcp.s3_sync import replicate_async
                    log_uri = latest_log.name
                    if log_uri.startswith("file://"):
                        log_uri = log_uri[len("file://"):]
                    replicate_async(Path(log_uri), user_id=user_id)
                except Exception:
                    pass
        except Exception as e:
            results_summary = {"error": f"Could not parse results: {str(e)}"}

        viewer_path = f"/results?group={run_id}" if run_id else "/results"
        viewer_base = os.environ.get("EVAL_VIEWER_URL", "http://localhost:4001")
        viewer_url = f"{viewer_base}{viewer_path}"

        # Auto-open the viewer so the user doesn't have to run a separate
        # command. On any failure we fall back to a manual-instructions string
        # rather than lying that the browser opened successfully.
        # The one-shot `run_evaluation_and_report` path passes openViewer=False
        # so it can open the viewer after the PDF report is written; otherwise
        # the page loads before the report exists and shows "no report yet".
        view_results_msg = f"Run `eval-mcp view` in your terminal, then open {viewer_url}"
        open_viewer = args.get("openViewer", True)
        if open_viewer:
            try:
                from eval_mcp.viewer import ensure_viewer_running
                info = ensure_viewer_running(port=4001, open_path=viewer_path)
                viewer_url = info["url"]
                if info.get("browserOpened"):
                    if info.get("alreadyRunning"):
                        view_results_msg = f"Viewer already running; opened {viewer_url}"
                    else:
                        view_results_msg = f"Started viewer and opened {viewer_url}"
                elif info.get("error"):
                    logger.warning(f"Viewer auto-start: {info['error']}")
                    view_results_msg = (
                        f"Could not auto-start viewer ({info['error']}). "
                        f"Run `eval-mcp view` manually, then open {viewer_url}"
                    )
            except Exception as e:
                logger.warning(f"Could not auto-start viewer: {e}")
        else:
            view_results_msg = None

        # Fail loud when every sample errored: this is the signal that
        # capture broke (e.g. OTel grandchild leak, missing creds, wrong
        # model ID). Previously this returned success=true with scores=[],
        # which let bugs ship unnoticed. The eval log is still produced so
        # the caller can dig in, but the response makes it unambiguous.
        if all_samples_errored:
            result = {
                "success": False,
                "evalId": eval_id,
                "configName": config_name,
                "runId": run_id,
                "viewerUrl": viewer_url,
                "userDir": str(user_dir),
                "error": (
                    "Evaluation produced no scores — every sample errored. "
                    "This usually means the agent failed to run (missing "
                    "credentials, invalid model ID, capture bug) rather than "
                    "actually scoring zero."
                ),
                "firstSampleError": first_sample_error,
                "summary": results_summary,
            }
            if view_results_msg is not None:
                result["viewResults"] = view_results_msg
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        result = {
            "success": True,
            "evalId": eval_id,
            "configName": config_name,
            "runId": run_id,
            "viewerUrl": viewer_url,
            "userDir": str(user_dir),
            "message": "Evaluation completed successfully",
            "summary": results_summary,
            "nextStep": (
                f"Call generate_report(group_id=\"{run_id}\") to create a PDF "
                f"report for the user. Pass `context` describing what they "
                f"were evaluating so the narrative is tailored."
            ) if run_id else None,
        }
        if view_results_msg is not None:
            result["viewResults"] = view_results_msg

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
        _refresh_keys_from_file()
        env = os.environ.copy()
        env["INSPECT_LOG_DIR"] = log_dir_str
        region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
        env["AWS_REGION"] = region
        env["AWS_DEFAULT_REGION"] = region

        retry_cmd = [
            *_INSPECT_CMD, "eval-retry",
            *[str(l) for l in logs_to_retry],
            "--adaptive-connections", "true",
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
            from eval_mcp.core.eval_results import precompute_eval_results
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
