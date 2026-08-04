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


def _task_file() -> Path:
    """Absolute path to the vendored aiwf task file."""
    from eval_mcp.benchmarks.aiwf import task as aiwf_task

    return Path(aiwf_task.__file__).resolve()


def _catalog() -> Dict[str, Dict[str, Any]]:
    """The bundled multi-turn benchmarks, keyed by task name."""
    from eval_mcp.benchmarks.aiwf import AIWF_TASKS, turns
    from eval_mcp.benchmarks.aiwf.data_loader import knowledge_base

    n_turns = len(turns())
    n_tool_turns = sum(1 for t in turns() if t.expected_tool)
    catalog: Dict[str, Dict[str, Any]] = {}
    for name in AIWF_TASKS:
        kb_chars = len(knowledge_base(name))
        catalog[name] = {
            "id": name,
            "title": (
                "AI Engineer World's Fair multi-turn conversation "
                f"({'~12K' if 'medium' in name else '~40K'}-token knowledge base)"
            ),
            "category": "Multi-turn",
            "turns": n_turns,
            "toolTurns": n_tool_turns,
            "knowledgeBaseChars": kb_chars,
            "approxContextTokens": kb_chars // 4,
            "dimensions": ["tool_use_correct", "instruction_following", "kb_grounding"],
            "headlineMetric": "turn_pass rate (all 3 dimensions pass on the same turn)",
            "source": "https://github.com/kwindla/aiewf-eval",
            "license": "MIT",
            "mode": "text",
        }
    return catalog


async def handle_list_multiturn_benchmarks(args: Dict[str, Any]) -> List[TextContent]:
    """List the bundled multi-turn benchmarks."""
    try:
        catalog = _catalog()
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": True,
                        "total": len(catalog),
                        "benchmarks": list(catalog.values()),
                        "note": (
                            "Text-mode port of kwindla/aiewf-eval. Each run is ONE "
                            "sample per model: a scripted 30-turn conversation where "
                            "context accumulates. Upstream's speech-to-speech "
                            "pipelines and its audio-derived turn_taking dimension "
                            "are not ported."
                        ),
                        "costNote": (
                            "Context grows every turn, so one 30-turn run costs "
                            "roughly 0.5M input tokens on aiwf_medium_context and "
                            "~3M on aiwf_long_context, per model. Start with medium."
                        ),
                        "hint": (
                            "run_multiturn_benchmark(task='aiwf_medium_context', "
                            "providers=[...]) — use max_turns for a cheap smoke run."
                        ),
                    },
                    indent=2,
                ),
            )
        ]
    except Exception as e:
        logger.exception("Failed to list multi-turn benchmarks")
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

        catalog = _catalog()
        if task not in catalog:
            return [
                _err(
                    f"Unknown multi-turn benchmark '{task}'. Available: "
                    f"{sorted(catalog)}. For the inspect_evals catalog use "
                    f"run_benchmark instead.",
                    eval_id,
                )
            ]

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
            f"{_task_file()}@{task}",
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
                            f"Multi-turn benchmark {task} completed. Headline metric "
                            f"is turn_pass (fraction of turns where all 3 dimensions "
                            f"pass). Call get_viewer_url or list_evaluations for "
                            f"results; per-turn detail is in the score metadata."
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
