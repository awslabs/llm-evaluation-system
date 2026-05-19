"""Prompt optimizer — closed-loop iteration of a prompt template.

Analog of skill-creator's ``run_loop.py``: starting from an initial prompt
template (with ``{question}`` placeholder), iteratively propose better
versions based on per-sample failures, then pick the winner by held-out
test score so the chosen prompt isn't overfit to the iteration sample.

## Simplification vs the plan

The plan called for each iteration to be a "real eval" via Inspect AI's
subprocess so it shows up in ``list_evaluations``. For v1 we instead
score in-process via direct Bedrock calls — same Jury, same criteria,
same per-criterion improvement-note capture, just no Inspect subprocess.
Reasons:

- ~10× faster per iteration (no subprocess startup, no log file IO).
- Reuses the proven pattern from ``generate_judge.refine_criteria_loop``.
- Iterations show up in the new optimizations tab; users who want a
  full-fledged eval entry for the winner can run
  ``create_eval_config(prompts=[winner])`` afterward.

## Anti-overfit

- Stratified random train/test split with a fixed seed.
- Optimizer LLM sees only train results — never test scores during
  iteration.
- Winner picked by **test** pass rate, not train.
- History feed includes prior attempts with explicit "don't repeat"
  framing so the model proposes structurally different variants.
- Full current prompt is passed to the optimizer (no truncation) so it
  can do targeted edits instead of rewrites.

## Crash safety

Any uncaught exception inside an iteration returns the most recent
known-good prompt + the partial history. A flaky round never wipes out
useful work.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from typing import Any, Dict, List, Optional, Tuple

from mcp.types import TextContent

from eval_mcp.core.bedrock_client import BedrockClient
from eval_mcp.core.judge_config import JUDGE_MODELS
from eval_mcp.core.user_storage import (
    get_dataset_by_name,
    get_judge_by_name,
    save_optimization_to_db,
)

logger = logging.getLogger(__name__)

# Loop knobs — env-var overridable so users can tune without code changes.
DEFAULT_MAX_ITERATIONS = int(os.environ.get("EVAL_MCP_OPTIMIZE_MAX_ITERATIONS", "3"))
DEFAULT_SAMPLE_SIZE = int(os.environ.get("EVAL_MCP_OPTIMIZE_SAMPLE_SIZE", "10"))
DEFAULT_TEST_HOLDOUT = float(os.environ.get("EVAL_MCP_OPTIMIZE_TEST_HOLDOUT", "0.4"))
RNG_SEED = 42  # Fixed so splits and history are reproducible across calls.

# Maximum prompt-side failures rendered into the optimizer's context. More
# than this and the LLM struggles to read it; we already have the aggregate
# pass rate as a separate signal.
MAX_FAILURES_IN_CONTEXT = 10


# ---------------------------------------------------------------------------
# Tool schemas — forced-tool output so we never have to parse free text
# ---------------------------------------------------------------------------


# Per-criterion scoring tool. Built dynamically because criterion names
# come from the judge config and we want each one to be a required field.
def _build_scoring_tool(criteria: List[Dict[str, str]]) -> Dict[str, Any]:
    properties: Dict[str, Any] = {}
    required: List[str] = []
    for c in criteria:
        properties[c["name"]] = {
            "type": "integer",
            "enum": [0, 1],
            "description": c["description"],
        }
        required.append(c["name"])
        properties[f"{c['name']}_improvement"] = {
            "type": "string",
            "description": (
                f"If {c['name']} scored 0, one short sentence on what the "
                "answer should change. Empty string when scored 1."
            ),
        }
    properties["reason"] = {
        "type": "string",
        "description": "One sentence summarizing the overall judgment.",
    }
    required.append("reason")
    return {
        "name": "submit_scores",
        "description": "Score each criterion 0 or 1 with per-criterion improvement notes.",
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


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
    "improvement notes from the judges.\n\n"
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
# Pure helpers — testable without Bedrock
# ---------------------------------------------------------------------------


def _split_train_test(
    qa_pairs: List[Dict[str, str]],
    holdout: float = DEFAULT_TEST_HOLDOUT,
    seed: int = RNG_SEED,
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Random split with a fixed seed. Floor of 1 per side when possible.

    Not stratified by class because QA pairs don't have a class label —
    'stratified' from the plan referred to should_trigger in skill-creator,
    which doesn't apply here. We just need a deterministic random split.
    """
    n = len(qa_pairs)
    if n == 0:
        return [], []
    if n == 1:
        # Can't split — use the single pair for both. Caller will get a
        # degenerate eval but at least it runs.
        return list(qa_pairs), list(qa_pairs)

    rng = random.Random(seed)
    shuffled = list(qa_pairs)
    rng.shuffle(shuffled)

    n_test = max(1, int(round(n * holdout)))
    n_test = min(n_test, n - 1)  # leave at least one for train
    test = shuffled[:n_test]
    train = shuffled[n_test:]
    return train, test


def _pick_winner(
    iteration_records: List[Dict[str, Any]],
    test_scores_by_iter: Dict[int, float],
) -> Tuple[int, str, float]:
    """Pick the highest-test-score iteration. Ties go to the earlier one
    so users get the simplest version of structurally-equivalent winners.

    ``iteration_records`` is the per-iter history (iter 0 = initial).
    ``test_scores_by_iter`` maps iter index -> pass rate on the test set.
    Returns ``(winner_iter, winner_prompt, winner_test_score)``.
    """
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
        # Nothing scored — fall back to initial.
        return 0, iteration_records[0]["prompt"], 0.0
    winner_prompt = next(r["prompt"] for r in iteration_records if r["iter"] == best_iter)
    return best_iter, winner_prompt, best_score


def _format_failures_for_optimizer(failures: List[Dict[str, Any]]) -> str:
    """Render per-sample failures into a compact text block for the
    optimizer LLM. Includes question, golden, model answer, and the
    failing criteria with their improvement notes."""
    lines: List[str] = []
    for i, f in enumerate(failures, start=1):
        lines.append(f"Sample {i}:")
        lines.append(f"  Q: {f['question'][:300]}")
        lines.append(f"  Golden: {f['golden'][:400]}")
        lines.append(f"  Model answer: {f['answer'][:400]}")
        failed = [c for c in f.get("criteria", []) if c.get("score") == 0]
        if failed:
            lines.append("  Failed criteria:")
            for c in failed:
                note = c.get("improvement_note", "")
                lines.append(f"    - {c['name']}: {note}" if note else f"    - {c['name']}")
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


# ---------------------------------------------------------------------------
# In-process eval: produce model answer for a question, then score with jury
# ---------------------------------------------------------------------------


async def _produce_answer(
    bedrock: BedrockClient,
    prompt_template: str,
    question: str,
) -> str:
    """Render the prompt with {question} substituted, send as user
    message to the model under test. Returns the model's text reply."""
    user_message = prompt_template.replace("{question}", question)
    response = await asyncio.to_thread(
        bedrock.create_message,
        messages=[{"role": "user", "content": user_message}],
        max_tokens=2048,
        temperature=0.0,
    )
    text_parts = [
        block.get("text", "")
        for block in response.get("content", [])
        if block.get("type") == "text"
    ]
    return "".join(text_parts).strip()


async def _score_one_with_jury(
    bedrock: BedrockClient,
    criteria: List[Dict[str, str]],
    question: str,
    golden: str,
    answer: str,
) -> Dict[str, Any]:
    """Run the same per-criterion judge call we use at eval time, but
    in-process. For v1 we use a single judge (Sonnet 4.6 via the
    BedrockClient singleton); multi-judge jury during the loop would 3×
    the per-sample cost for a signal the optimizer doesn't really need.
    Returns ``{criterion_name: {"score": 0|1, "improvement": "..."}}``.
    """
    tool = _build_scoring_tool(criteria)
    criteria_block = "\n".join(f"- {c['name']}: {c['description']}" for c in criteria)
    user_prompt = (
        "Score the AI answer against each criterion. For criteria scored 0, "
        "fill in the corresponding <criterion>_improvement field with ONE "
        "short sentence on what the answer should change.\n\n"
        f"Criteria:\n{criteria_block}\n\n"
        f"[Question]\n{question}\n\n"
        f"[AI Answer]\n{answer}\n\n"
        f"[Reference Answer]\n{golden}\n\n"
        "Call submit_scores with your assessment."
    )
    response = await asyncio.to_thread(
        bedrock.create_message,
        messages=[{"role": "user", "content": user_prompt}],
        tools=[tool],
        tool_choice={"type": "tool", "name": "submit_scores"},
        max_tokens=2000,
        temperature=0.0,
    )
    for block in response.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "submit_scores":
            args = block.get("input", {}) or {}
            out: Dict[str, Any] = {}
            for c in criteria:
                name = c["name"]
                out[name] = {
                    "score": int(bool(args.get(name, 0))),
                    "improvement": str(args.get(f"{name}_improvement", "") or "").strip(),
                }
            return out
    raise ValueError("Judge returned no submit_scores tool_use")


async def _eval_one_sample(
    bedrock: BedrockClient,
    prompt_template: str,
    criteria: List[Dict[str, str]],
    qa_pair: Dict[str, str],
) -> Dict[str, Any]:
    """Full eval of a single QA pair: produce answer, score it. Returns
    a row ready for failure aggregation."""
    answer = await _produce_answer(bedrock, prompt_template, qa_pair["question"])
    scored = await _score_one_with_jury(
        bedrock, criteria, qa_pair["question"], qa_pair["golden_answer"], answer
    )
    n_criteria = len(criteria)
    passes = sum(1 for c in scored.values() if c["score"] == 1)
    sample_score = passes / n_criteria if n_criteria else 0.0
    criteria_rows = [
        {
            "name": c["name"],
            "score": scored[c["name"]]["score"],
            "improvement_note": scored[c["name"]]["improvement"],
        }
        for c in criteria
    ]
    return {
        "question": qa_pair["question"],
        "golden": qa_pair["golden_answer"],
        "answer": answer,
        "sample_score": sample_score,
        "criteria": criteria_rows,
    }


async def _eval_samples(
    bedrock: BedrockClient,
    prompt_template: str,
    criteria: List[Dict[str, str]],
    samples: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    """Eval many samples in parallel. Individual failures are caught and
    excluded from the result rather than aborting the whole batch."""
    tasks = [_eval_one_sample(bedrock, prompt_template, criteria, s) for s in samples]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    rows: List[Dict[str, Any]] = []
    for sample, r in zip(samples, results):
        if isinstance(r, Exception):
            logger.warning("Dropping sample during iteration eval: %s", r)
            continue
        rows.append(r)
    return rows


def _pass_rate(rows: List[Dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    return sum(r["sample_score"] for r in rows) / len(rows)


def _select_failures(rows: List[Dict[str, Any]], max_n: int = MAX_FAILURES_IN_CONTEXT) -> List[Dict[str, Any]]:
    """Pick the most-failing samples to feed the optimizer. Sorted by
    sample_score ascending, capped at ``max_n``. Filters out samples that
    fully passed (those have nothing to teach the optimizer)."""
    failing = [r for r in rows if r["sample_score"] < 1.0]
    failing.sort(key=lambda r: r["sample_score"])
    return failing[:max_n]


# ---------------------------------------------------------------------------
# Optimizer LLM call
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
        temperature=0.4,  # Slight stochasticity helps explore prompt space.
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
    qa_pairs: List[Dict[str, str]],
    criteria: List[Dict[str, str]],
    initial_prompt: str,
    max_iter: int = DEFAULT_MAX_ITERATIONS,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    test_holdout: float = DEFAULT_TEST_HOLDOUT,
) -> Dict[str, Any]:
    """Run the optimization loop. Returns the full record ready for
    persistence — does NOT save it (the handler does that, so unit tests
    can exercise the loop without storage)."""

    train, test = _split_train_test(qa_pairs, holdout=test_holdout)
    if not train:
        # Degenerate dataset; bail with the initial prompt as winner.
        return {
            "initial_prompt": initial_prompt,
            "winner_prompt": initial_prompt,
            "winner_iter": 0,
            "winner_test_score": 0.0,
            "history": [{"iter": 0, "prompt": initial_prompt, "train_pass_rate": 0.0, "n_train_samples": 0}],
            "test_scores_by_iter": {},
            "train_size": 0,
            "test_size": 0,
            "status": "no_train_data",
            "rationales": {},
        }

    last_good = initial_prompt
    history: List[Dict[str, Any]] = []
    rationales: Dict[int, str] = {}
    status = "complete"

    # Iter 0 = initial prompt; score it on train so the optimizer has a
    # baseline AND so the test-time comparison includes the initial.
    sample_n = min(sample_size, len(train))
    rng = random.Random(RNG_SEED)
    initial_sample = rng.sample(train, sample_n) if sample_n < len(train) else list(train)
    try:
        initial_rows = await _eval_samples(bedrock, last_good, criteria, initial_sample)
        initial_train_rate = _pass_rate(initial_rows)
    except Exception as e:
        logger.warning("Initial eval failed: %s. Bailing with initial prompt.", e)
        return {
            "initial_prompt": initial_prompt,
            "winner_prompt": initial_prompt,
            "winner_iter": 0,
            "winner_test_score": 0.0,
            "history": [{"iter": 0, "prompt": initial_prompt, "train_pass_rate": 0.0, "n_train_samples": 0}],
            "test_scores_by_iter": {},
            "train_size": len(train),
            "test_size": len(test),
            "status": f"error_initial: {type(e).__name__}",
            "rationales": {},
        }

    history.append({
        "iter": 0,
        "prompt": initial_prompt,
        "train_pass_rate": initial_train_rate,
        "n_train_samples": len(initial_rows),
    })

    if initial_train_rate >= 1.0:
        # Already perfect on train — no point iterating.
        status = "converged_initial"

    for i in range(1, max_iter + 1):
        if status.startswith("converged"):
            break
        try:
            # Use the most recent iteration's rows as the failures to
            # show the optimizer. We don't re-eval — initial_rows was
            # just produced if iter 1; iter 2+ uses the rows from the
            # previous iteration's eval.
            current_rows = history[-1].get("_rows") or initial_rows
            failures = _select_failures(current_rows)
            if not failures:
                status = "converged"
                break

            proposal = await _propose_new_prompt(
                bedrock, last_good, failures, history[:-0] if False else history
            )
            candidate = proposal["new_prompt"]
            rationales[i] = proposal["rationale"]

            # Eval the candidate on a fresh random train sample to
            # measure if it's actually better.
            cand_sample = rng.sample(train, sample_n) if sample_n < len(train) else list(train)
            cand_rows = await _eval_samples(bedrock, candidate, criteria, cand_sample)
            cand_train_rate = _pass_rate(cand_rows)

            history.append({
                "iter": i,
                "prompt": candidate,
                "train_pass_rate": cand_train_rate,
                "n_train_samples": len(cand_rows),
                "_rows": cand_rows,  # transient — used for next iter's failures
            })
            last_good = candidate

            if cand_train_rate >= 1.0:
                status = "converged"
                break
        except Exception as e:
            logger.warning("Iter %d failed: %s. Stopping early with last-good.", i, e)
            status = f"partial: {type(e).__name__}"
            break

    # Strip transient _rows so they don't get persisted.
    for h in history:
        h.pop("_rows", None)

    # Test-time ranking: evaluate every prompt in history against the
    # held-out test set. Run in parallel by prompt.
    test_scores_by_iter: Dict[int, float] = {}
    if test:
        async def _score_one_prompt(rec: Dict[str, Any]) -> Tuple[int, float]:
            try:
                rows = await _eval_samples(bedrock, rec["prompt"], criteria, test)
                return rec["iter"], _pass_rate(rows)
            except Exception as e:
                logger.warning("Test eval for iter %d failed: %s", rec["iter"], e)
                return rec["iter"], 0.0

        results = await asyncio.gather(*(_score_one_prompt(h) for h in history))
        test_scores_by_iter = {i: s for i, s in results}

    winner_iter, winner_prompt, winner_test_score = _pick_winner(
        history, test_scores_by_iter
    )

    return {
        "initial_prompt": initial_prompt,
        "winner_prompt": winner_prompt,
        "winner_iter": winner_iter,
        "winner_test_score": winner_test_score,
        "history": history,
        "test_scores_by_iter": test_scores_by_iter,
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
        qa_pairs=qa_pairs,
        criteria=criteria,
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
            {"iter": h["iter"], "train_pass_rate": h["train_pass_rate"]}
            for h in result["history"]
        ],
        "test_size": result["test_size"],
        "train_size": result["train_size"],
    }
    return [TextContent(type="text", text=json.dumps(summary, indent=2))]
