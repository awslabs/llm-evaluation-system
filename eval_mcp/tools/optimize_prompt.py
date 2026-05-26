"""Prompt optimizer — closed-loop iteration of a prompt template.

Analog of skill-creator's ``run_loop.py``: starting from an initial prompt
template (with ``{question}`` placeholder), iteratively propose better
versions based on per-sample failures from the multi-judge jury, then
pick the winner by held-out test score so the chosen prompt isn't
overfit to the iteration sample.

## How a single iteration works

Each iteration is a real Inspect AI evaluation — same subprocess, same
``jury_scorer()``, same ``.eval`` log, same provider validation as any
other eval in this system. The optimizer never duplicates scoring code;
it routes through the same pipeline a hand-run benchmark would.

1. Sample N pairs from the train split.
2. Write them to a temp dataset file in the user dir.
3. Build a task file via ``create_inspect_task_file`` with the current
   prompt template + judge config + providers.
4. Spawn ``inspect eval`` as a subprocess.
5. Read the ``.eval`` log: per sample, pull score and
   ``metadata['criteria_results']`` (with per-criterion improvement notes
   from each judge that scored 0).
6. Feed failures to the optimizer LLM, get a new prompt, recurse.

Each iteration produces a real ``run_id`` stored in the optimization
record. From the "Prompts Optimized" tab the user can click an iteration
and jump straight to its evaluation in Results.

## Anti-overfit

- Random train/test split with a fixed seed.
- Optimizer LLM sees only train results — never test scores during the
  loop.
- Winner picked by **test** pass rate, not train.
- History feed shows prior attempts with explicit "don't repeat" framing
  so the model proposes structurally different variants.
- Full current prompt is passed to the optimizer (no truncation) so it
  can do targeted edits instead of rewrites.

## Crash safety

Any uncaught exception inside an iteration returns the most recent
known-good prompt plus the partial history. A flaky subprocess never
wipes out useful work.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mcp.types import TextContent

from inspect_ai._view.common import list_eval_logs_async
from inspect_ai.log import read_eval_log_async

from eval_mcp.core.bedrock_client import BedrockClient
from eval_mcp.core.judge_config import JUDGE_MODELS, JudgeConfig
from eval_mcp.core.user_storage import (
    get_dataset_by_name,
    get_judge_by_name,
    get_user_dir,
    get_user_log_dir,
    save_optimization_to_db,
)
from eval_mcp.tools.create_config import create_inspect_task_file
from eval_mcp.tools.external_providers import _refresh_keys_from_file
from eval_mcp.tools.run_eval import (
    _running_evaluations,
    _terminate_process_gracefully,
)

logger = logging.getLogger(__name__)

# Loop knobs — env-var overridable so users can tune without code changes.
DEFAULT_MAX_ITERATIONS = int(os.environ.get("EVAL_MCP_OPTIMIZE_MAX_ITERATIONS", "3"))
DEFAULT_SAMPLE_SIZE = int(os.environ.get("EVAL_MCP_OPTIMIZE_SAMPLE_SIZE", "10"))
DEFAULT_TEST_HOLDOUT = float(os.environ.get("EVAL_MCP_OPTIMIZE_TEST_HOLDOUT", "0.4"))
RNG_SEED = 42

# Maximum prompt-side failures rendered into the optimizer's context. More
# than this and the LLM struggles to read it; the aggregate pass rate is
# already a separate signal in the history block.
MAX_FAILURES_IN_CONTEXT = 10

# Per-iter subprocess timeout. Inspect runs of 10 samples × 3 judges
# typically complete in 30-90s; the cap is generous so flaky Bedrock
# retries don't bite.
ITER_SUBPROCESS_TIMEOUT_S = int(os.environ.get("EVAL_MCP_OPTIMIZE_ITER_TIMEOUT_S", "900"))


OPTIMIZER_TOOL: Dict[str, Any] = {
    "name": "submit_prompt",
    "description": "Submit a proposed new prompt template.",
    "input_schema": {
        "type": "object",
        "properties": {
            "new_prompt": {
                "type": "string",
                "description": (
                    "The new prompt template. MUST contain '{question}' "
                    "somewhere so the eval harness can substitute the user's "
                    "question. Edit the existing prompt — do not compress a "
                    "long carefully-tuned prompt into a short one."
                ),
            },
            "rationale": {
                "type": "string",
                "description": "One short paragraph explaining what you changed and why.",
            },
        },
        "required": ["new_prompt"],
    },
}


_OPTIMIZER_SYSTEM_PROMPT = (
    "You are improving a prompt template used to evaluate an LLM against a "
    "dataset. The template wraps each user question and is fed as the user "
    "message to a model under test. You see the current template, the "
    "failures it produced on a sample of questions, and per-criterion "
    "improvement notes from each judge in the jury.\n\n"
    "Your job is to propose a new template that addresses the failures.\n\n"
    "Rules:\n"
    "- PRESERVE STRUCTURE ON LONG PROMPTS. If the current prompt is "
    "multi-paragraph or has numbered protocols / explicit sections, EDIT "
    "it — don't rewrite from scratch. Add lines, tweak constraints, "
    "reorder. Do not compress a long carefully-tuned prompt into a short "
    "one without strong evidence the structure itself is wrong.\n"
    "- GENERALIZE, DON'T OVERFIT. The failures are samples; your new "
    "prompt will be evaluated on a held-out test set. Avoid baking in "
    "specifics from the training failures.\n"
    "- KEEP {question} SOMEWHERE in the output. It's the placeholder for "
    "the user's question; without it the template can't run.\n"
    "- DO NOT REPEAT PRIOR ATTEMPTS. History shows what's been tried — if "
    "the same shape keeps failing, try something structurally different."
)


# ---------------------------------------------------------------------------
# Pure helpers — testable without Bedrock or Inspect subprocess
# ---------------------------------------------------------------------------


def _split_train_test(
    qa_pairs: List[Dict[str, str]],
    holdout: float = DEFAULT_TEST_HOLDOUT,
    seed: int = RNG_SEED,
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Random split with a fixed seed. Floor of 1 per side when possible."""
    n = len(qa_pairs)
    if n == 0:
        return [], []
    if n == 1:
        return list(qa_pairs), list(qa_pairs)

    rng = random.Random(seed)
    shuffled = list(qa_pairs)
    rng.shuffle(shuffled)

    n_test = max(1, int(round(n * holdout)))
    n_test = min(n_test, n - 1)
    test = shuffled[:n_test]
    train = shuffled[n_test:]
    return train, test


def _pick_winner(
    iteration_records: List[Dict[str, Any]],
    test_scores_by_iter: Dict[int, float],
) -> Tuple[int, str, float]:
    """Pick the highest-test-score iteration. Ties go to the earlier one
    so users get the simplest version of structurally-equivalent winners."""
    best_iter = -1
    best_score = -1.0
    for rec in iteration_records:
        i = rec["iter"]
        score = test_scores_by_iter.get(i)
        if score is None:
            continue
        if score > best_score or (score == best_score and i < best_iter):
            best_iter = i
            best_score = score
    if best_iter < 0:
        return 0, iteration_records[0]["prompt"], 0.0
    winner_prompt = next(r["prompt"] for r in iteration_records if r["iter"] == best_iter)
    return best_iter, winner_prompt, best_score


def _format_failures_for_optimizer(failures: List[Dict[str, Any]]) -> str:
    """Render per-sample failures for the optimizer LLM. Each failing
    criterion shows the jury vote count and the per-judge improvement
    notes captured by ``jury_scorer``'s ``metadata['criteria_results']``."""
    lines: List[str] = []
    for i, f in enumerate(failures, start=1):
        lines.append(f"Sample {i}:")
        lines.append(f"  Q: {f['question'][:300]}")
        lines.append(f"  Golden: {f['golden'][:400]}")
        lines.append(f"  Model answer: {f['answer'][:400]}")
        failed = [c for c in f.get("criteria_results", []) if c.get("score", 1.0) < 1.0]
        if failed:
            lines.append("  Failed criteria (jury):")
            for c in failed:
                vf = c.get("votes_for", 0)
                tot = c.get("total", 0)
                lines.append(f"    - {c['name']} ({vf}/{tot} judges passed):")
                for note_entry in c.get("improvement_notes", []) or []:
                    judge = note_entry.get("judge", "?")
                    note = note_entry.get("note", "")
                    if note:
                        lines.append(f"        [{judge}] {note}")
        lines.append("")
    return "\n".join(lines)


def _format_history_for_optimizer(history: List[Dict[str, Any]]) -> str:
    if not history:
        return "(no previous attempts)"
    lines: List[str] = []
    for h in history:
        lines.append(
            f"Iter {h['iter']} — train pass rate {h['train_pass_rate']:.2f}:"
        )
        prompt_preview = h["prompt"]
        if len(prompt_preview) > 800:
            prompt_preview = prompt_preview[:800] + "  …[truncated]"
        lines.append(prompt_preview)
        lines.append("")
    return "\n".join(lines)


def _select_failures(rows: List[Dict[str, Any]], max_n: int = MAX_FAILURES_IN_CONTEXT) -> List[Dict[str, Any]]:
    """Pick the most-failing samples to feed the optimizer. Samples that
    fully passed (sample_score == 1.0) are dropped — nothing to teach."""
    failing = [r for r in rows if r["sample_score"] < 1.0]
    failing.sort(key=lambda r: r["sample_score"])
    return failing[:max_n]


def _pass_rate(rows: List[Dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    return sum(r["sample_score"] for r in rows) / len(rows)


# ---------------------------------------------------------------------------
# Inspect AI subprocess: per-iteration eval + final test-time ranking
# ---------------------------------------------------------------------------


_INSPECT_CMD = [sys.executable, "-m", "inspect_ai"]


def _safe_name_fragment(s: str) -> str:
    """Sanitize a string for use inside an Inspect config filename. Same
    constraints as ``run_eval._VALID_CONFIG_NAME_PATTERN`` — alphanumerics,
    underscores, dashes only."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", s)[:64]


def _write_temp_dataset(user_dir: Path, fragment: str, samples: List[Dict[str, str]]) -> Path:
    """Write a subset of QA pairs to ``temp/<fragment>.json`` in the user
    directory. Inspect's ``json_dataset`` reads from this path."""
    temp_dir = user_dir / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    out_path = temp_dir / f"{fragment}.json"
    inspect_samples = [
        {"question": s["question"], "golden_answer": s["golden_answer"]}
        for s in samples
    ]
    out_path.write_text(json.dumps(inspect_samples, indent=2))
    return out_path


def _write_inspect_config(
    user_dir: Path,
    config_name: str,
    dataset_path: Path,
    providers: List[str],
    judge_config: JudgeConfig,
    prompts: Optional[List[str]],
    description: str,
) -> Path:
    """Generate task file + config JSON via ``create_inspect_task_file``
    and write both to ``<user_dir>/configs/``. Returns the .py path."""
    config_dir = user_dir / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)

    task_code, config_data = create_inspect_task_file(
        dataset_path=str(dataset_path),
        providers=providers,
        config_name=config_name,
        config_dir=str(config_dir),
        judge_config=judge_config,
        description=description,
        prompts=prompts,
    )
    py_path = config_dir / f"{config_name}.py"
    py_path.write_text(task_code)
    (config_dir / f"{config_name}.json").write_text(json.dumps(config_data, indent=2))
    return py_path


async def _snapshot_log_set(log_dir: str) -> set:
    """Return the set of ``.eval`` log paths currently in ``log_dir``.

    Used to compute the delta after an ``inspect eval`` subprocess
    finishes — Inspect names logs after the task function (e.g.
    ``eval_task``, ``eval_1``), not after our config file, so we identify
    the run's logs by what's new rather than by filename matching.
    """
    try:
        infos = await list_eval_logs_async(log_dir)
        return {info.name for info in infos}
    except Exception:
        return set()


async def _spawn_inspect_eval(
    user_id: str,
    user_dir: Path,
    config_name: str,
    providers: List[str],
    log_dir: str,
) -> None:
    """Run ``inspect eval`` as a subprocess.

    Reuses the same flags as ``handle_run_evaluation`` (adaptive
    parallelism, no-fail-on-error, log-shared) but skips the user-facing
    ceremony — no browser pop-up, no provider validation (the dataset's
    providers were validated when the optimization started), no S3 sync
    per iter, no retry pass. Those are correct for an interactive eval;
    inside a tight loop they're wasted work.

    Registers the subprocess in ``run_eval._running_evaluations`` so the
    chat backend's stop button (which calls ``cancel_user_evaluation``)
    can kill the entire process group. Without this, hitting stop
    mid-optimization left orphaned Inspect subprocesses that kept
    writing to the user's storage while subsequent requests read it.
    """
    _refresh_keys_from_file()
    env = os.environ.copy()
    env["INSPECT_LOG_DIR"] = log_dir
    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
    env["AWS_REGION"] = region
    env["AWS_DEFAULT_REGION"] = region

    relative_task = f"configs/{config_name}.py"
    cmd: List[str] = [
        *_INSPECT_CMD, "eval",
        relative_task,
        "--adaptive-connections", "true",
        "--no-log-images",
        "--no-fail-on-error",
        "--log-shared", "10",
    ]
    if providers:
        cmd.extend(["--model", ",".join(providers)])

    logger.info("optimizer subprocess: %s", shlex.join(cmd))
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=str(user_dir),
        start_new_session=True,
    )
    # Register so ``cancel_user_evaluation`` can SIGTERM the process
    # group. The eval_id is descriptive enough that the chat backend's
    # cancel response surfaces "optimizer iter X" rather than a bare
    # config name.
    _running_evaluations[user_id] = {
        "process": process,
        "eval_id": f"optim_{config_name}",
        "config_name": config_name,
    }
    try:
        try:
            await asyncio.wait_for(process.wait(), timeout=ITER_SUBPROCESS_TIMEOUT_S)
        except asyncio.TimeoutError:
            await _terminate_process_gracefully(process)
            raise RuntimeError(
                f"inspect eval timed out after {ITER_SUBPROCESS_TIMEOUT_S}s for {config_name}"
            )
        except asyncio.CancelledError:
            # Chat backend cancelled the task. Kill the process group
            # so we don't leak inspect subprocesses, then re-raise so
            # the optimizer loop can record status="partial".
            await _terminate_process_gracefully(process)
            raise
    finally:
        _running_evaluations.pop(user_id, None)

    # Always capture stderr — even on success — so we can include it in
    # error context when the subprocess "succeeds" but produces no log
    # (silent capture-pipeline failures used to look identical to "still
    # running" before we surfaced this).
    stderr_bytes = b""
    if process.stderr:
        try:
            stderr_bytes = await asyncio.wait_for(process.stderr.read(), timeout=5)
        except asyncio.TimeoutError:
            pass
    stderr = stderr_bytes.decode("utf-8", errors="replace")[:1500]

    if process.returncode != 0:
        raise RuntimeError(
            f"inspect eval exited {process.returncode} for {config_name}: {stderr}"
        )
    if stderr:
        # Subprocess succeeded but Inspect printed warnings — keep them
        # for the caller's diagnostic logging.
        logger.debug("inspect eval stderr (rc=0) for %s: %s", config_name, stderr)


def _extract_rows_from_log(log: Any) -> List[Dict[str, Any]]:
    """Pull per-sample rows out of an Inspect eval log. Each row carries
    the question, golden, model answer, jury score, and the
    ``criteria_results`` metadata where improvement notes live.

    The optimizer always writes its temp configs with the default
    ``scorers=["jury"]``, but read defensively: prefer the ``jury_scorer``
    entry by name in case other scorers were composed in. Built-in
    scorers (``f1``/``exact``/...) carry no ``criteria_results`` metadata,
    so they're useless to the optimizer; we fall back to the first
    available scorer only so a malformed log doesn't crash."""
    rows: List[Dict[str, Any]] = []
    for sample in (log.samples or []):
        score_obj = None
        if sample.scores:
            score_obj = sample.scores.get("jury_scorer") or next(
                iter(sample.scores.values()), None
            )

        sample_score = 0.0
        criteria_results: List[Dict[str, Any]] = []
        if score_obj is not None:
            try:
                sample_score = float(score_obj.value) if score_obj.value is not None else 0.0
            except (TypeError, ValueError):
                sample_score = 0.0
            meta = getattr(score_obj, "metadata", None) or {}
            criteria_results = meta.get("criteria_results", []) or []

        answer = ""
        if sample.output and sample.output.completion:
            answer = sample.output.completion

        rows.append({
            "question": str(sample.input),
            "golden": str(sample.target),
            "answer": answer,
            "sample_score": sample_score,
            "criteria_results": criteria_results,
        })
    return rows


async def _new_logs_since(log_dir: str, before: set) -> List[str]:
    """Return ``.eval`` log paths that appeared since ``before`` snapshot."""
    after = await list_eval_logs_async(log_dir)
    return [info.name for info in after if info.name not in before]


async def _run_inspect_iteration(
    user_id: str,
    user_dir: Path,
    log_dir: str,
    config_name: str,
    samples: List[Dict[str, str]],
    prompt_template: str,
    providers: List[str],
    judge_config: JudgeConfig,
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Single optimizer iteration as a real Inspect AI eval. Returns
    ``(run_id, rows)`` where ``rows`` is per-sample data ready for failure
    selection. Raises on subprocess failure — the loop catches and
    falls back to last-good."""
    dataset_path = _write_temp_dataset(user_dir, config_name, samples)
    _write_inspect_config(
        user_dir=user_dir,
        config_name=config_name,
        dataset_path=dataset_path,
        providers=providers,
        judge_config=judge_config,
        prompts=[prompt_template],
        description=f"Optimizer iteration: {config_name}",
    )

    before = await _snapshot_log_set(log_dir)
    await _spawn_inspect_eval(user_id, user_dir, config_name, providers, log_dir)
    new_logs = await _new_logs_since(log_dir, before)
    if not new_logs:
        raise RuntimeError(
            f"inspect eval succeeded but produced no .eval log for {config_name} "
            f"(log_dir={log_dir})"
        )
    # Single-prompt iteration: exactly one task = one log. If Inspect
    # for some reason wrote more than one, take the one with task name
    # matching our convention.
    log = await read_eval_log_async(new_logs[0])
    return log.eval.run_id, _extract_rows_from_log(log)


async def _run_inspect_test_ranking(
    user_id: str,
    user_dir: Path,
    log_dir: str,
    optimization_id: str,
    test_samples: List[Dict[str, str]],
    prompts_by_iter: List[Tuple[int, str]],
    providers: List[str],
    judge_config: JudgeConfig,
) -> Tuple[Optional[str], Dict[int, float]]:
    """Run every prompt in history against the test split in a single
    Inspect job. ``prompts_by_iter`` preserves iteration order so we can
    match the resulting ``eval_1/eval_2/...`` task logs back to iters.
    Returns ``(run_id, test_pass_rate_per_iter)``."""
    config_name = f"opt_{_safe_name_fragment(optimization_id)}_test"
    dataset_path = _write_temp_dataset(user_dir, config_name, test_samples)

    # create_inspect_task_file generates eval_1, eval_2, ... when given
    # multiple prompts. Order is preserved, so prompts_by_iter[i] maps to
    # the (i+1)-th task.
    prompts_in_order = [p for _, p in prompts_by_iter]
    _write_inspect_config(
        user_dir=user_dir,
        config_name=config_name,
        dataset_path=dataset_path,
        providers=providers,
        judge_config=judge_config,
        prompts=prompts_in_order,
        description=f"Optimizer test-time ranking: {optimization_id}",
    )

    before = await _snapshot_log_set(log_dir)
    await _spawn_inspect_eval(user_id, user_dir, config_name, providers, log_dir)
    new_logs = await _new_logs_since(log_dir, before)
    if not new_logs:
        return None, {}

    # Each new log corresponds to one task (eval_1, eval_2, ...). Read
    # each, match by task name to the prompt index, compute pass rate.
    scores_by_iter: Dict[int, float] = {}
    run_id: Optional[str] = None
    iter_order = [i for i, _ in prompts_by_iter]
    for path in new_logs:
        try:
            log = await read_eval_log_async(path)
        except Exception as e:
            logger.warning("Could not read test log %s: %s", path, e)
            continue
        run_id = log.eval.run_id or run_id
        task_name = (log.eval.task or "").rsplit("/", 1)[-1]
        m = re.match(r"^eval_(\d+)$", task_name)
        if not m:
            continue
        idx = int(m.group(1)) - 1  # 1-based in template, 0-based here
        if idx < 0 or idx >= len(iter_order):
            continue
        iter_key = iter_order[idx]
        rows = _extract_rows_from_log(log)
        scores_by_iter[iter_key] = _pass_rate(rows)

    return run_id, scores_by_iter


# ---------------------------------------------------------------------------
# Optimizer LLM call — proposes a new prompt from failures + history
# ---------------------------------------------------------------------------


async def _propose_new_prompt(
    bedrock: BedrockClient,
    current_prompt: str,
    failures: List[Dict[str, Any]],
    history: List[Dict[str, Any]],
) -> Dict[str, str]:
    """Ask the optimizer LLM for a new prompt. Returns
    ``{"new_prompt": str, "rationale": str}`` or raises ``ValueError``
    if the tool call was malformed."""
    user_prompt = (
        f"CURRENT PROMPT TEMPLATE:\n```\n{current_prompt}\n```\n\n"
        f"FAILURES ON TRAINING SAMPLE ({len(failures)} samples shown):\n"
        f"{_format_failures_for_optimizer(failures)}\n"
        f"PREVIOUS ATTEMPTS (do NOT repeat — try something different):\n"
        f"{_format_history_for_optimizer(history)}\n\n"
        "Call submit_prompt with your proposal."
    )
    response = await asyncio.to_thread(
        bedrock.create_message,
        messages=[{"role": "user", "content": user_prompt}],
        system=_OPTIMIZER_SYSTEM_PROMPT,
        tools=[OPTIMIZER_TOOL],
        tool_choice={"type": "tool", "name": "submit_prompt"},
        max_tokens=4000,
        temperature=0.4,
    )
    for block in response.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "submit_prompt":
            args = block.get("input", {}) or {}
            new = (args.get("new_prompt") or "").strip()
            if not new:
                raise ValueError("Optimizer returned empty new_prompt")
            if "{question}" not in new:
                raise ValueError("Optimizer dropped the {question} placeholder")
            return {
                "new_prompt": new,
                "rationale": (args.get("rationale") or "").strip(),
            }
    raise ValueError("Optimizer call returned no submit_prompt tool_use")


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


async def optimize_prompt_loop(
    bedrock: BedrockClient,
    user_id: str,
    optimization_id: str,
    qa_pairs: List[Dict[str, str]],
    judge_config: JudgeConfig,
    providers: List[str],
    initial_prompt: str,
    max_iter: int = DEFAULT_MAX_ITERATIONS,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    test_holdout: float = DEFAULT_TEST_HOLDOUT,
) -> Dict[str, Any]:
    """Run the optimization loop. Each iteration is a real Inspect AI
    eval; iteration ``eval_run_id`` is stored in history so the UI can
    deep-link to it. Returns the full record ready for persistence."""

    train, test = _split_train_test(qa_pairs, holdout=test_holdout)
    if not train:
        return {
            "initial_prompt": initial_prompt,
            "winner_prompt": initial_prompt,
            "winner_iter": 0,
            "winner_test_score": 0.0,
            "history": [{"iter": 0, "prompt": initial_prompt, "train_pass_rate": 0.0, "n_train_samples": 0, "eval_run_id": None}],
            "test_scores_by_iter": {},
            "test_run_id": None,
            "train_size": 0,
            "test_size": 0,
            "status": "no_train_data",
            "rationales": {},
        }

    user_dir = get_user_dir(user_id)
    log_dir = get_user_log_dir(user_id)
    if not log_dir.startswith("s3://"):
        Path(log_dir).mkdir(parents=True, exist_ok=True)

    safe_id = _safe_name_fragment(optimization_id)
    last_good = initial_prompt
    history: List[Dict[str, Any]] = []
    rationales: Dict[int, str] = {}
    status = "complete"
    last_rows: List[Dict[str, Any]] = []

    sample_n = min(sample_size, len(train))
    rng = random.Random(RNG_SEED)

    # Iter 0: score the initial prompt on a train sample.
    iter_0_sample = rng.sample(train, sample_n) if sample_n < len(train) else list(train)
    iter_0_config = f"opt_{safe_id}_iter_0"
    try:
        run_id_0, iter_0_rows = await _run_inspect_iteration(
            user_id=user_id,
            user_dir=user_dir,
            log_dir=log_dir,
            config_name=iter_0_config,
            samples=iter_0_sample,
            prompt_template=initial_prompt,
            providers=providers,
            judge_config=judge_config,
        )
        last_rows = iter_0_rows
        initial_train_rate = _pass_rate(iter_0_rows)
    except Exception as e:
        logger.warning("Initial eval failed: %s. Bailing with initial prompt.", e)
        return {
            "initial_prompt": initial_prompt,
            "winner_prompt": initial_prompt,
            "winner_iter": 0,
            "winner_test_score": 0.0,
            "history": [{"iter": 0, "prompt": initial_prompt, "train_pass_rate": 0.0, "n_train_samples": 0, "eval_run_id": None}],
            "test_scores_by_iter": {},
            "test_run_id": None,
            "train_size": len(train),
            "test_size": len(test),
            "status": f"error_initial: {type(e).__name__}",
            "rationales": {},
        }

    history.append({
        "iter": 0,
        "prompt": initial_prompt,
        "train_pass_rate": initial_train_rate,
        "n_train_samples": len(iter_0_rows),
        "eval_run_id": run_id_0,
    })

    if initial_train_rate >= 1.0:
        status = "converged_initial"

    for i in range(1, max_iter + 1):
        if status.startswith("converged"):
            break
        try:
            failures = _select_failures(last_rows)
            if not failures:
                status = "converged"
                break

            proposal = await _propose_new_prompt(bedrock, last_good, failures, history)
            candidate = proposal["new_prompt"]
            rationales[i] = proposal["rationale"]

            cand_sample = rng.sample(train, sample_n) if sample_n < len(train) else list(train)
            cand_config = f"opt_{safe_id}_iter_{i}"
            cand_run_id, cand_rows = await _run_inspect_iteration(
                user_id=user_id,
                user_dir=user_dir,
                log_dir=log_dir,
                config_name=cand_config,
                samples=cand_sample,
                prompt_template=candidate,
                providers=providers,
                judge_config=judge_config,
            )
            cand_rate = _pass_rate(cand_rows)

            history.append({
                "iter": i,
                "prompt": candidate,
                "train_pass_rate": cand_rate,
                "n_train_samples": len(cand_rows),
                "eval_run_id": cand_run_id,
            })
            last_good = candidate
            last_rows = cand_rows

            if cand_rate >= 1.0:
                status = "converged"
                break
        except Exception as e:
            logger.warning("Iter %d failed: %s. Stopping early with last-good.", i, e)
            status = f"partial: {type(e).__name__}"
            break

    # Test-time ranking — one Inspect run evaluating every prompt in history
    # against the held-out test split. The .eval logs land in list_evaluations
    # so a curious user can drill into the test scores per prompt.
    test_run_id: Optional[str] = None
    test_scores_by_iter: Dict[int, float] = {}
    if test and len(history) > 0:
        try:
            test_run_id, test_scores_by_iter = await _run_inspect_test_ranking(
                user_id=user_id,
                user_dir=user_dir,
                log_dir=log_dir,
                optimization_id=optimization_id,
                test_samples=test,
                prompts_by_iter=[(h["iter"], h["prompt"]) for h in history],
                providers=providers,
                judge_config=judge_config,
            )
        except Exception as e:
            logger.warning("Test-time ranking failed: %s. Falling back to train scores.", e)
            test_scores_by_iter = {h["iter"]: h["train_pass_rate"] for h in history}

    winner_iter, winner_prompt, winner_test_score = _pick_winner(history, test_scores_by_iter)

    return {
        "initial_prompt": initial_prompt,
        "winner_prompt": winner_prompt,
        "winner_iter": winner_iter,
        "winner_test_score": winner_test_score,
        "history": history,
        "test_scores_by_iter": test_scores_by_iter,
        "test_run_id": test_run_id,
        "train_size": len(train),
        "test_size": len(test),
        "status": status,
        "rationales": rationales,
    }


# ---------------------------------------------------------------------------
# MCP handler
# ---------------------------------------------------------------------------


async def handle_optimize_prompt(
    bedrock: BedrockClient, args: Dict[str, Any]
) -> List[TextContent]:
    """Resolve dataset + judge, run the loop, persist the record,
    return a summary TextContent."""
    user_id = args.get("user_id")
    dataset_name = args.get("dataset")
    judge_name = args.get("judge")
    initial_prompt = args.get("initial_prompt", "{question}")
    providers = args.get("providers") or []
    max_iter = int(args.get("max_iterations", DEFAULT_MAX_ITERATIONS))
    sample_size = int(args.get("sample_size", DEFAULT_SAMPLE_SIZE))
    test_holdout = float(args.get("test_holdout", DEFAULT_TEST_HOLDOUT))

    def _err(msg: str) -> List[TextContent]:
        return [TextContent(type="text", text=json.dumps({"success": False, "error": msg}))]

    if not user_id:
        return _err("user_id is required")
    if not dataset_name:
        return _err("dataset is required — use list_datasets to see what's available")
    if not judge_name:
        return _err("judge is required — use list_judges to see what's available")
    if not providers:
        return _err("providers is required — at least one model under test")
    if "{question}" not in initial_prompt:
        return _err("initial_prompt must contain {question} placeholder")

    dataset = get_dataset_by_name(user_id, dataset_name)
    if not dataset:
        return _err(f"Dataset '{dataset_name}' not found")
    judge = get_judge_by_name(user_id, judge_name)
    if not judge:
        return _err(f"Judge '{judge_name}' not found")

    criteria = (judge.get("config") or {}).get("criteria") or []
    if not criteria:
        return _err(f"Judge '{judge_name}' has no criteria")

    # Build the JudgeConfig the same way create_eval_config does, so the
    # jury_scorer template renders with identical judges + criteria as a
    # normal eval would.
    judge_models_arg = args.get("judge_models")
    custom_judges = {m: m for m in judge_models_arg} if judge_models_arg else None
    judge_config = JudgeConfig(criteria=criteria, judges=custom_judges)

    qa_pairs = [
        {"question": t["vars"]["question"], "golden_answer": t["vars"]["golden_answer"]}
        for t in dataset.get("tests", [])
    ]
    if len(qa_pairs) < 2:
        return _err("Dataset needs at least 2 QA pairs for train/test split")

    optimization_id = f"opt_{int(time.time() * 1000)}"
    started_at = int(time.time() * 1000)

    result = await optimize_prompt_loop(
        bedrock=bedrock,
        user_id=user_id,
        optimization_id=optimization_id,
        qa_pairs=qa_pairs,
        judge_config=judge_config,
        providers=providers,
        initial_prompt=initial_prompt,
        max_iter=max_iter,
        sample_size=sample_size,
        test_holdout=test_holdout,
    )

    record = {
        "id": optimization_id,
        "created_at": started_at,
        "dataset": dataset_name,
        "judge": judge_name,
        "providers": providers,
        "max_iterations": max_iter,
        "sample_size": sample_size,
        "test_holdout": test_holdout,
        **result,
    }
    save_optimization_to_db(user_id, record)

    summary = {
        "success": True,
        "optimization_id": optimization_id,
        "status": result["status"],
        "winner_iter": result["winner_iter"],
        "winner_test_score": result["winner_test_score"],
        "winner_prompt": result["winner_prompt"],
        "train_pass_rate_per_iter": [
            {"iter": h["iter"], "train_pass_rate": h["train_pass_rate"], "eval_run_id": h.get("eval_run_id")}
            for h in result["history"]
        ],
        "test_run_id": result.get("test_run_id"),
        "test_size": result["test_size"],
        "train_size": result["train_size"],
    }
    return [TextContent(type="text", text=json.dumps(summary, indent=2))]
