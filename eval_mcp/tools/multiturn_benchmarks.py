"""Run the bundled multi-turn benchmarks (``eval_mcp/benchmarks/``).

Separate from ``tools/benchmarks.py``, which wraps the installed
``inspect_evals`` catalog and can only run what that package registers. These
tasks ship with us, so they're launched by absolute path to the vendored task
file — verified to work from any cwd, which matters because the subprocess runs
in the user's storage dir.

Everything downstream is shared with the other runners: same ``_INSPECT_CMD``
(so ``inspect_patches`` applies), same per-user registry so ``cancel_evaluation``
works, same log dir so results reach the viewer and S3 sync.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.types import TextContent

from eval_mcp.core.bedrock_client import raise_if_autodetect_error, resolve_region
from eval_mcp.core.user_storage import get_user_dir, get_user_log_dir
from eval_mcp.tools.external_providers import _refresh_keys_from_file
from eval_mcp.tools.run_eval import (
    _INSPECT_CMD,
    _running_evaluations,
    _terminate_process_gracefully,
    _validate_providers,
)

logger = logging.getLogger(__name__)

# A 30-turn conversation per model, plus one judge call over the whole
# transcript. Slowest observed single model is a few minutes; 2h leaves room for
# a wide model sweep on a throttled account without hanging forever.
_TIMEOUT_SECONDS = 2 * 60 * 60


async def handle_list_multiturn_benchmarks(args: Dict[str, Any]) -> List[TextContent]:
    """List the bundled benchmarks, with their per-task cost and caveats.

    ``list_benchmarks`` also surfaces these (flagged ``bundled: true``) mixed in
    with the inspect_evals catalog; this tool is the drill-down that returns the
    full ``eval.yaml`` for each without paging through 130 upstream entries.
    """
    try:
        from eval_mcp.benchmarks.registry import discover

        benches = discover()
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": True,
                        "total": len(benches),
                        "benchmarks": [b.details() for b in benches.values()],
                        "hint": (
                            "run_benchmark(task=<task name>, providers=[...]). "
                            "Pass max_turns for a cheap smoke run, judge_model to "
                            "override the judge. These need no HuggingFace "
                            "download, optional extra or sandbox."
                        ),
                    },
                    indent=2,
                ),
            )
        ]
    except Exception as e:
        logger.exception("Failed to list bundled benchmarks")
        return [
            TextContent(
                type="text",
                text=json.dumps({"success": False, "error": f"Failed to list: {e}"}),
            )
        ]


async def handle_run_multiturn_benchmark(args: Dict[str, Any]) -> List[TextContent]:
    """Run one bundled multi-turn benchmark against one or more models."""
    process: Optional[asyncio.subprocess.Process] = None
    eval_id = f"mtbench_{int(time.time() * 1000)}"
    user_id = args.get("user_id")
    try:
        task = args.get("task")
        providers = args.get("providers") or []
        judge_model = args.get("judge_model")
        max_turns = args.get("max_turns")

        if not user_id:
            return [_err("user_id is required", eval_id)]
        if not task:
            return [_err("task is required", eval_id)]
        if not providers:
            return [
                _err(
                    "At least one provider is required "
                    "(e.g. ['bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0']).",
                    eval_id,
                )
            ]

        from eval_mcp.benchmarks.registry import all_task_names, resolve

        hit = resolve(task)
        if hit is None:
            return [
                _err(
                    f"Unknown bundled benchmark '{task}'. Available: "
                    f"{sorted(all_task_names())}. For the inspect_evals catalog, "
                    f"run_benchmark handles those too.",
                    eval_id,
                )
            ]
        bench, task = hit

        if max_turns is not None:
            try:
                max_turns = int(max_turns)
            except (TypeError, ValueError):
                return [_err(f"max_turns must be an integer, got {max_turns!r}", eval_id)]
            if max_turns < 1:
                return [_err("max_turns must be >= 1", eval_id)]

        # Fail fast on an unusable model rather than 30 turns deep. Same gate
        # run_evaluation uses, so the error text is identical and actionable.
        if any(p.startswith(("bedrock/", "openai/bedrock/")) for p in providers):
            raise_if_autodetect_error()
            validation = await _validate_providers(providers)
            if not validation.get("valid"):
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "success": False,
                                "evalId": eval_id,
                                "task": task,
                                "error": "One or more models failed validation.",
                                "failedProviders": validation.get("failed_providers"),
                            },
                            indent=2,
                        ),
                    )
                ]

        user_dir = get_user_dir(user_id)
        os.makedirs(user_dir, exist_ok=True)
        log_dir_str = get_user_log_dir(user_id)
        if not log_dir_str.startswith("s3://"):
            Path(log_dir_str).mkdir(parents=True, exist_ok=True)

        _refresh_keys_from_file()
        env = os.environ.copy()
        env["INSPECT_LOG_DIR"] = log_dir_str
        region = resolve_region()
        env["AWS_REGION"] = region
        env["AWS_DEFAULT_REGION"] = region
        if max_turns is not None:
            # Read by the solver. A CLI -T arg would work too, but this keeps
            # the smoke-run knob out of the task's public signature.
            env["EVAL_MCP_AIWF_MAX_TURNS"] = str(max_turns)

        cmd: List[str] = [
            *_INSPECT_CMD,
            "eval",
            f"{bench.task_file}@{task}",
            "--model",
            ",".join(providers),
            "--adaptive-connections",
            "true",
            "--no-log-images",
            "--no-fail-on-error",
            "--log-shared",
            "10",
        ]
        # No --max-tokens: _INSPECT_CMD routes through eval_mcp._inspect_main so
        # the Bedrock provider omits it and each model gets its own default. A
        # 30-turn conversation is exactly where a 2048 cap produces empty
        # completions from reasoning models.
        if judge_model:
            cmd.extend(["-T", f"judge_model={judge_model}"])

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(user_dir),
            start_new_session=True,
        )
        logger.info(
            "Started multi-turn benchmark %s (pid %s) for user %s",
            task, process.pid, user_id,
        )
        _running_evaluations[user_id] = {
            "process": process,
            "eval_id": eval_id,
            "config_name": task,
        }

        try:
            await asyncio.wait_for(process.wait(), timeout=_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await _terminate_process_gracefully(process)
            return [
                _err(f"Benchmark timed out after {_TIMEOUT_SECONDS}s.", eval_id, task)
            ]
        finally:
            _running_evaluations.pop(user_id, None)

        stderr_str = ""
        if process.returncode != 0 and process.stderr:
            try:
                b = await asyncio.wait_for(process.stderr.read(), timeout=5)
                stderr_str = b.decode("utf-8") if b else ""
            except Exception:
                stderr_str = "(stderr unavailable)"

        try:
            from eval_mcp.core.eval_results import precompute_eval_results

            await precompute_eval_results(user_id)
        except Exception as e:
            logger.warning("precompute failed: %s", e)

        if process.returncode != 0:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "success": False,
                            "evalId": eval_id,
                            "task": task,
                            "error": f"Benchmark failed with exit code {process.returncode}",
                            "stderr": stderr_str[:2000],
                        },
                        indent=2,
                    ),
                )
            ]

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": True,
                        "evalId": eval_id,
                        "task": task,
                        "models": providers,
                        "message": (
                            f"Benchmark {task} completed. Headline metric is "
                            f"{bench.headline_metric or 'the first reported metric'}. "
                            f"Call get_viewer_url or list_evaluations for results; "
                            f"per-sample detail is in the score metadata."
                        ),
                    },
                    indent=2,
                ),
            )
        ]

    except Exception as e:
        logger.exception("Multi-turn benchmark failed")
        if user_id:
            _running_evaluations.pop(user_id, None)
        if process and process.returncode is None:
            await _terminate_process_gracefully(process)
        return [_err(f"Benchmark failed: {e}", eval_id)]


def _err(message: str, eval_id: str, task: Optional[str] = None) -> TextContent:
    payload: Dict[str, Any] = {"success": False, "evalId": eval_id, "error": message}
    if task:
        payload["task"] = task
    return TextContent(type="text", text=json.dumps(payload, indent=2))
