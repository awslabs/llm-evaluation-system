"""Premade benchmark discovery + execution (UK AISI ``inspect_evals``).

Three tools, designed for *progressive disclosure* so the agent never has
to hold the full 129-benchmark catalog in context:

- ``list_benchmarks``       — filtered/paginated compact rows (discover)
- ``get_benchmark_details`` — full info for ONE benchmark (drill in)
- ``run_benchmark``         — run ``inspect_evals/<task>`` via Inspect AI

The catalog comes from ``inspect_evals.metadata.load_listing()``, which reads
the package's bundled YAML and does NOT import the benchmark modules — so
listing is cheap (~100ms) and never triggers a HuggingFace/torch import.

Two capability flags are surfaced on every entry so the agent (and the user)
know up front what a benchmark needs:

- ``needs_extra``   — the benchmark declares an optional-dependency group
  (``inspect_evals[<group>]``). ~34 of 129. Running it without the extra
  installed fails with an ImportError, so we flag it rather than crash.
- ``needs_sandbox`` — the benchmark is ``isolated`` (runs untrusted code in a
  container). 7 of 129 (cve_bench, kernelbench, mle_bench, …). Needs Docker
  locally or inspect-k8s-sandbox on EKS.

Execution reuses run_eval.py's process machinery (graceful termination, the
per-user running-eval registry, the catastrophic-failure detector) so a
benchmark run cancels and reports identically to a normal eval. Results land
in the same INSPECT_LOG_DIR and flow to the same viewer.
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.types import TextContent

from eval_mcp.core.bedrock_client import raise_if_autodetect_error
from eval_mcp.core.user_storage import get_user_dir, get_user_log_dir
from eval_mcp.tools.external_providers import _refresh_keys_from_file
from eval_mcp.tools.run_eval import (
    _INSPECT_CMD,
    _running_evaluations,
    _terminate_process_gracefully,
    is_catastrophic_eval_failure,
)

logger = logging.getLogger(__name__)

# Task names are Python identifiers exposed via inspect_evals' entry points;
# the registry path is always ``inspect_evals/<task_name>``. Validate before
# interpolating into a subprocess argument so a caller can't smuggle a path or
# shell metacharacter through the ``task`` field.
_VALID_TASK_NAME = re.compile(r"^[a-zA-Z0-9_]+$")

# 1h is plenty for the no-sandbox Q&A benchmarks we run by default; agentic /
# sandboxed ones can run far longer, but those need infra we gate on anyway.
_BENCHMARK_TIMEOUT_SECONDS = 60 * 60


def _load_evals() -> List[Any]:
    """Return the list of EvalListing entries, or raise a clean error.

    Importing ``inspect_evals.metadata`` is cheap (YAML only). We import it
    lazily inside the handlers so a broken/absent install surfaces as a tool
    error rather than an MCP-startup import failure.
    """
    from inspect_evals.metadata import load_listing

    return list(load_listing().evals)


def _sandbox_requirement(e: Any) -> Dict[str, Any]:
    """Detect whether a benchmark needs a code-execution sandbox, and where.

    The ``isolated`` metadata flag is unreliable for this — only 7 of 129
    benchmarks set it, yet ~42 actually run untrusted code (humaneval, mbpp,
    ds1000, bigcodebench, swe_bench, …). The authoritative signal is
    ``runtime_metadata.sandbox``, a list of the phases that need a sandbox
    (e.g. ``["scorer"]`` for HumanEval, ``["solver"]`` for SWE-bench).

    ``supports_k8s`` matters for our EKS deployment: code benchmarks that are
    Docker-only (HumanEval/MBPP, ``supports_k8s=False``) cannot run on the
    inspect-k8s-sandbox we provide in the cluster, so we must say so up front
    rather than letting the run die at the scoring step.

    Returns: {needs: bool, phases: list, supportsK8s: bool|None}.
    """
    rm = {}
    try:
        rm = e.model_dump().get("runtime_metadata") or {}
    except Exception:
        rm = {}
    phases = rm.get("sandbox") or []
    # Fall back to the legacy isolated flag if runtime_metadata is absent.
    needs = bool(phases) or bool(getattr(e, "isolated", False))
    return {
        "needs": needs,
        "phases": list(phases),
        "supportsK8s": rm.get("supports_k8s"),
    }


def _entry_summary(e: Any) -> Dict[str, Any]:
    """Compact projection of one catalog entry — what ``list_benchmarks``
    returns per row. Deliberately small to keep agent context lean."""
    tasks = [t.name for t in (e.tasks or [])]
    samples = None
    for t in (e.tasks or []):
        s = getattr(t, "dataset_samples", None)
        if s:
            samples = s
            break
    sb = _sandbox_requirement(e)
    return {
        "id": e.id,
        "title": e.title,
        "category": e.group,
        "tasks": tasks,
        "sampleCount": samples,
        "needsExtra": bool(e.dependency or e.dependency_group),
        "needsSandbox": sb["needs"],
        "sandboxSupportsK8s": sb["supportsK8s"],
    }


def _matches(e: Any, search: Optional[str], category: Optional[str]) -> bool:
    if category and (e.group or "").lower() != category.lower():
        return False
    if search:
        hay = " ".join(
            [e.id or "", e.title or "", e.description or "", e.group or ""]
            + [t.name for t in (e.tasks or [])]
        ).lower()
        if search.lower() not in hay:
            return False
    return True


async def handle_list_benchmarks(args: Dict[str, Any]) -> List[TextContent]:
    """List premade benchmarks, filtered + paginated."""
    try:
        search = args.get("search")
        category = args.get("category")
        limit = int(args.get("limit", 20))
        offset = int(args.get("offset", 0))

        evals = _load_evals()
        matched = [e for e in evals if _matches(e, search, category)]
        matched.sort(key=lambda e: ((e.group or "~"), e.id or ""))

        page = matched[offset : offset + limit]
        rows = [_entry_summary(e) for e in page]

        # Category histogram helps the agent narrow a follow-up call instead
        # of paging blindly through everything.
        categories: Dict[str, int] = {}
        for e in evals:
            categories[e.group or "Uncategorized"] = categories.get(e.group or "Uncategorized", 0) + 1

        payload = {
            "success": True,
            "total": len(matched),
            "totalCatalog": len(evals),
            "offset": offset,
            "limit": limit,
            "hasMore": offset + limit < len(matched),
            "nextOffset": offset + limit if offset + limit < len(matched) else None,
            "categories": dict(sorted(categories.items(), key=lambda x: -x[1])),
            "benchmarks": rows,
            "hint": (
                "Call get_benchmark_details(benchmark_id) for one benchmark's "
                "task variants and requirements, then run_benchmark(task=...). "
                "needsExtra/needsSandbox benchmarks require extra setup."
            ),
        }
        return [TextContent(type="text", text=json.dumps(payload, indent=2))]
    except ModuleNotFoundError:
        return [TextContent(type="text", text=json.dumps({
            "success": False,
            "error": "inspect_evals is not installed. It ships as a core dependency; reinstall the package.",
        }))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({
            "success": False, "error": f"Failed to list benchmarks: {e}",
        }))]


async def handle_get_benchmark_details(args: Dict[str, Any]) -> List[TextContent]:
    """Full detail for a single benchmark (by id)."""
    try:
        benchmark_id = args.get("benchmark_id")
        if not benchmark_id:
            return [TextContent(type="text", text=json.dumps({
                "success": False, "error": "benchmark_id is required",
            }))]

        evals = _load_evals()
        e = next((x for x in evals if x.id == benchmark_id), None)
        if e is None:
            # Offer near-matches so the agent can self-correct without a full
            # re-list.
            near = [x.id for x in evals if benchmark_id.lower() in (x.id or "").lower()][:5]
            return [TextContent(type="text", text=json.dumps({
                "success": False,
                "error": f"Benchmark '{benchmark_id}' not found.",
                "didYouMean": near,
            }))]

        tasks = [
            {"name": t.name, "sampleCount": getattr(t, "dataset_samples", None)}
            for t in (e.tasks or [])
        ]
        datasets = []
        for a in (e.external_assets or []):
            if getattr(a, "type", None) == "huggingface":
                datasets.append({"type": "huggingface", "source": getattr(a, "source", None)})

        extra_group = e.dependency_group or e.dependency
        payload = {
            "success": True,
            "id": e.id,
            "title": e.title,
            "description": (e.description or "").strip(),
            "category": e.group,
            "arxiv": str(e.arxiv) if e.arxiv else None,
            "tasks": tasks,
            "datasets": datasets,
            "needsExtra": bool(extra_group),
            "extraInstall": f'pip install "inspect_evals[{extra_group}]"' if extra_group else None,
            "needsSandbox": _sandbox_requirement(e)["needs"],
            "runHint": (
                f"run_benchmark(task=\"{tasks[0]['name']}\", providers=[...]) "
                if tasks else "No runnable task is registered for this entry."
            ),
        }
        sb = _sandbox_requirement(e)
        if sb["needs"]:
            phases = ", ".join(sb["phases"]) or "execution"
            if sb["supportsK8s"]:
                payload["sandboxNote"] = (
                    f"This benchmark runs untrusted code in a sandbox (phase: {phases}). "
                    f"Needs Docker locally, or inspect-k8s-sandbox on EKS (set "
                    f"INSPECT_SANDBOX_TYPE=k8s)."
                )
            else:
                payload["sandboxNote"] = (
                    f"This benchmark runs untrusted code in a Docker sandbox (phase: "
                    f"{phases}) and is NOT supported on Kubernetes (supports_k8s=false). "
                    f"It can only run locally with Docker — it will fail in the EKS "
                    f"deployment. Consider a custom coding eval (generate_qa_pairs + "
                    f"generate_judge) instead."
                )
        return [TextContent(type="text", text=json.dumps(payload, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({
            "success": False, "error": f"Failed to get benchmark details: {e}",
        }))]


def _resolve_task(task: str, evals: List[Any]) -> Dict[str, Any]:
    """Map a user-supplied ``task`` to a runnable registry name + flags.

    Accepts either a task name (``mmlu_0_shot``) or a benchmark id
    (``gsm8k``). When given an id whose only task shares the id we run it
    directly; when an id has multiple variants we refuse and list them so the
    caller picks one explicitly.
    """
    # Direct task-name match.
    for e in evals:
        for t in (e.tasks or []):
            if t.name == task:
                return {"ok": True, "task": task, "entry": e}
    # Benchmark-id match → resolve to its task(s).
    entry = next((e for e in evals if e.id == task), None)
    if entry:
        names = [t.name for t in (entry.tasks or [])]
        if len(names) == 1:
            return {"ok": True, "task": names[0], "entry": entry}
        return {
            "ok": False,
            "error": (
                f"'{task}' is a benchmark with multiple task variants: {names}. "
                f"Pass one of them as `task`."
            ),
        }
    return {"ok": False, "error": f"Unknown benchmark/task '{task}'. Use list_benchmarks to discover names."}


async def handle_run_benchmark(args: Dict[str, Any]) -> List[TextContent]:
    """Run a premade ``inspect_evals`` benchmark via subprocess.

    Mirrors run_eval.handle_run_evaluation's process lifecycle (graceful
    termination, per-user registry, results read) but targets a registry task
    name instead of a generated config file.
    """
    process: Optional[asyncio.subprocess.Process] = None
    eval_id = f"bench_{int(time.time() * 1000)}"
    try:
        task = args.get("task")
        user_id = args.get("user_id")
        providers = args.get("providers") or []
        limit = args.get("limit")
        task_args = args.get("task_args") or {}

        if not user_id:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": "user_id is required"}))]
        if not task:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": "task is required"}))]
        if not providers:
            return [TextContent(type="text", text=json.dumps({
                "success": False,
                "error": "At least one provider is required (e.g. ['bedrock/us.anthropic.claude-sonnet-4-6']).",
            }))]

        # Resolve id-or-task → runnable task name, and pick up its flags.
        try:
            evals = _load_evals()
        except ModuleNotFoundError:
            return [TextContent(type="text", text=json.dumps({
                "success": False, "error": "inspect_evals is not installed.",
            }))]
        resolved = _resolve_task(task, evals)
        if not resolved["ok"]:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": resolved["error"]}))]
        task = resolved["task"]
        entry = resolved["entry"]

        if not _VALID_TASK_NAME.match(task):
            return [TextContent(type="text", text=json.dumps({
                "success": False, "error": f"Invalid task name '{task}'.",
            }))]

        # Fail-fast guards: don't launch a run that can't possibly capture.
        extra_group = entry.dependency_group or entry.dependency
        if extra_group:
            # Verify the extra is importable; if not, surface the install line.
            import importlib
            try:
                importlib.import_module(f"inspect_evals.{entry.id}")
            except Exception:
                return [TextContent(type="text", text=json.dumps({
                    "success": False,
                    "error": f"Benchmark '{entry.id}' needs an optional dependency group.",
                    "extraInstall": f'pip install "inspect_evals[{extra_group}]"',
                }))]
        # Sandbox fail-fast. Use runtime_metadata.sandbox (authoritative: ~42
        # benchmarks), NOT the unreliable `isolated` flag (only 7). Without this,
        # a code-execution benchmark like HumanEval launches, downloads its
        # dataset, generates completions, then dies at the scoring step when it
        # tries to exec generated code in a sandbox that isn't there — exactly
        # the "failed after downloading the dataset" symptom.
        sb = _sandbox_requirement(entry)
        if sb["needs"]:
            sandbox_type = os.environ.get("INSPECT_SANDBOX_TYPE")
            phases = ", ".join(sb["phases"]) or "execution"
            # Docker-only benchmarks (supports_k8s=False) cannot run on the
            # EKS k8s-sandbox at all — reject regardless of INSPECT_SANDBOX_TYPE.
            if sb["supportsK8s"] is False and sandbox_type in (None, "k8s"):
                return [TextContent(type="text", text=json.dumps({
                    "success": False,
                    "error": (
                        f"Benchmark '{entry.id}' runs untrusted code in a Docker "
                        f"sandbox (phase: {phases}) and does NOT support Kubernetes "
                        f"(supports_k8s=false), so it cannot run in this deployment. "
                        f"It only works locally with Docker. For a model comparison "
                        f"here, use a custom coding eval instead: generate_qa_pairs "
                        f"(coding tasks) -> generate_judge -> create_eval_config -> "
                        f"run_evaluation."
                    ),
                    "needsSandbox": True,
                    "sandboxSupportsK8s": False,
                }))]
            if not sandbox_type:
                return [TextContent(type="text", text=json.dumps({
                    "success": False,
                    "error": (
                        f"Benchmark '{entry.id}' runs untrusted code in a sandbox "
                        f"(phase: {phases}) and no sandbox is configured. Needs Docker "
                        f"locally, or inspect-k8s-sandbox on EKS (set "
                        f"INSPECT_SANDBOX_TYPE=k8s)."
                    ),
                    "needsSandbox": True,
                }))]

        # Bedrock reachability: surface the multi-profile autodetect error here
        # rather than letting it fail deep in the subprocess.
        if any(p.startswith("bedrock/") for p in providers):
            raise_if_autodetect_error()

        user_dir = get_user_dir(user_id)
        os.makedirs(user_dir, exist_ok=True)
        log_dir_str = get_user_log_dir(user_id)
        if not log_dir_str.startswith("s3://"):
            Path(log_dir_str).mkdir(parents=True, exist_ok=True)

        _refresh_keys_from_file()
        env = os.environ.copy()
        env["INSPECT_LOG_DIR"] = log_dir_str
        region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
        env["AWS_REGION"] = region
        env["AWS_DEFAULT_REGION"] = region

        cmd: List[str] = [
            *_INSPECT_CMD, "eval",
            f"inspect_evals/{task}",
            "--model", ",".join(providers),
            "--adaptive-connections", "true",
            "--no-log-images",
            "--no-fail-on-error",
            "--log-shared", "10",
        ]
        if limit:
            cmd.extend(["--limit", str(int(limit))])
        for k, v in task_args.items():
            # -T key=value; inspect parses scalars/JSON on the far side.
            cmd.extend(["-T", f"{k}={v}"])

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(user_dir),
            start_new_session=True,
        )
        logger.info(f"Started benchmark {task} (pid {process.pid}) for user {user_id}")
        _running_evaluations[user_id] = {
            "process": process, "eval_id": eval_id, "config_name": f"inspect_evals/{task}",
        }

        try:
            await asyncio.wait_for(process.wait(), timeout=_BENCHMARK_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await _terminate_process_gracefully(process)
            _running_evaluations.pop(user_id, None)
            return [TextContent(type="text", text=json.dumps({
                "success": False, "evalId": eval_id, "task": task,
                "error": f"Benchmark timed out after {_BENCHMARK_TIMEOUT_SECONDS}s.",
            }))]
        finally:
            _running_evaluations.pop(user_id, None)

        stderr_str = ""
        if process.returncode != 0 and process.stderr:
            try:
                b = await asyncio.wait_for(process.stderr.read(), timeout=5)
                stderr_str = b.decode("utf-8") if b else ""
            except Exception:
                stderr_str = "(stderr unavailable)"

        # Pre-compute viewer comparison + sync, same as the standard runner.
        try:
            from eval_mcp.core.eval_results import precompute_eval_results
            await precompute_eval_results(user_id)
        except Exception as e:
            logger.warning(f"precompute failed: {e}")

        if process.returncode != 0:
            return [TextContent(type="text", text=json.dumps({
                "success": False, "evalId": eval_id, "task": task,
                "error": f"Benchmark failed with exit code {process.returncode}",
                "stderr": stderr_str[:2000],
            }, indent=2))]

        # Read results from the newest log (shape matches run_eval.py).
        results_summary = None
        run_id = None
        all_samples_errored = False
        try:
            from inspect_ai._view.common import list_eval_logs_async
            from inspect_ai.log import read_eval_log_async
            logs = await list_eval_logs_async(log_dir_str)
            if logs:
                latest = logs[0]
                log = await read_eval_log_async(latest.name, header_only=True)
                scores = []
                if log.results and log.results.scores:
                    for s in log.results.scores:
                        scores.append({"scorer": s.name, "metrics": {n: m.value for n, m in s.metrics.items()}})
                results_summary = {
                    "totalTests": log.eval.dataset.samples if log.eval.dataset else 0,
                    "scores": scores,
                    "logFile": latest.name,
                }
                run_id = log.eval.run_id
                all_samples_errored = is_catastrophic_eval_failure(scores, log.results)
        except Exception as e:
            results_summary = {"error": f"Could not parse results: {e}"}

        viewer_path = f"/results?group={run_id}" if run_id else "/results"
        viewer_base = os.environ.get("EVAL_VIEWER_URL", "http://localhost:4001")
        viewer_url = f"{viewer_base}{viewer_path}"

        if all_samples_errored:
            return [TextContent(type="text", text=json.dumps({
                "success": False, "evalId": eval_id, "task": task, "runId": run_id,
                "viewerUrl": viewer_url,
                "error": "Benchmark produced no scores — every sample errored (check model access / dataset download).",
                "summary": results_summary,
            }, indent=2))]

        return [TextContent(type="text", text=json.dumps({
            "success": True, "evalId": eval_id, "task": task, "runId": run_id,
            "viewerUrl": viewer_url, "summary": results_summary,
            "message": f"Benchmark inspect_evals/{task} completed.",
        }, indent=2))]

    except asyncio.CancelledError:
        if process is not None:
            await _terminate_process_gracefully(process)
        _running_evaluations.pop(args.get("user_id"), None)
        return [TextContent(type="text", text=json.dumps({
            "success": False, "evalId": eval_id, "task": args.get("task"),
            "error": "Benchmark was cancelled.",
        }))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({
            "success": False, "error": f"Failed to run benchmark: {e}",
        }))]
