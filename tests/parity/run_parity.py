#!/usr/bin/env python3
"""DeepEval ↔ eval-mcp RAG-scorer parity runner.

Runs the 10-sample reference dataset through BOTH our RAG scorers and
DeepEval's equivalents using the SAME Bedrock judge model, then reports
per-metric Spearman rank correlation between the two systems.

The point isn't absolute score equivalence — the math diverges in ways
that are explicit and documented (single-call vs two-stage; groundedness
vs hallucination rate; one context field vs two; see ``eval_mcp/scorers/rag.py``
docstring). What we expect to MATCH is the ordering:

    when DeepEval scores sample A higher than sample B on faithfulness,
    eval-mcp should agree.

Spearman correlation of ≥ 0.7 per metric on a 10-sample probe is strong
evidence that we're measuring the same construct. < 0.5 means we've
diverged from DeepEval in a way that needs investigation.

This script is NOT part of the pytest suite — it consumes Bedrock budget
and takes minutes per run. Run it on demand when you want to re-verify
parity after changing scorer prompts or aggregation logic.

Usage::

    # Install DeepEval into the dev venv first:
    .venv/bin/pip install deepeval boto3

    # Then run with a Bedrock judge model id from list_bedrock_models:
    BEDROCK_MODEL_ID="us.anthropic.claude-haiku-4-5-20251001-v1:0" \\
    AWS_REGION=us-west-2 \\
    .venv/bin/python tests/parity/run_parity.py

Outputs::

    Per-metric Spearman correlation table + per-sample side-by-side scores.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Spearman rank correlation — pure-Python (no scipy dependency).
# ---------------------------------------------------------------------------


def _rank(xs: list[float]) -> list[float]:
    """Average-rank assignment (1-based). Ties get the average of their positions.

    Standard Spearman behaviour — matches scipy.stats.rankdata(..., method='average').
    """
    indexed = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and xs[indexed[j + 1]] == xs[indexed[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # 1-based
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float:
    """Spearman rank correlation. Returns NaN-as-zero when degenerate."""
    assert len(xs) == len(ys), "lists must be same length"
    n = len(xs)
    if n < 2:
        return 0.0
    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = sum((rx[i] - mx) ** 2 for i in range(n))
    dy = sum((ry[i] - my) ** 2 for i in range(n))
    denom = (dx * dy) ** 0.5
    if denom == 0:
        # All ranks equal in one series → no signal. Report 0 not NaN.
        return 0.0
    return num / denom


# ---------------------------------------------------------------------------
# eval-mcp scorer adapter — drive our scorers directly without Inspect's
# subprocess machinery. Builds a stub TaskState that satisfies what our
# `async def score(state, target)` callable reads.
# ---------------------------------------------------------------------------


class _Target:
    def __init__(self, text: str) -> None:
        self.text = text


class _Output:
    def __init__(self, completion: str) -> None:
        self.completion = completion


class _StubState:
    """Stand-in for inspect_ai.solver.TaskState.

    Our scorers read: state.input, state.output.completion, state.metadata.
    target arrives as a separate arg. That's all they touch.
    """

    def __init__(self, question: str, actual_output: str, metadata: dict) -> None:
        self.input = question
        self.output = _Output(actual_output)
        self.metadata = metadata


async def _run_eval_mcp(sample: dict, judge_model: str) -> dict[str, float]:
    """Run all six RAG scorers on a single sample. Returns {metric: score}."""
    from eval_mcp.scorers.rag import (
        answer_relevancy,
        configure_judge,
        contextual_precision,
        contextual_recall,
        contextual_relevancy,
        faithfulness,
    )

    configure_judge(judge_model)
    state = _StubState(
        question=sample["question"],
        actual_output=sample["actual_output"],
        metadata={"retrieval_context": sample["retrieval_context"]},
    )
    target = _Target(sample["golden_answer"])

    scorers = {
        "faithfulness": faithfulness(),
        "answer_relevancy": answer_relevancy(),
        "contextual_precision": contextual_precision(),
        "contextual_recall": contextual_recall(),
        "contextual_relevancy": contextual_relevancy(),
    }

    out: dict[str, float] = {}
    for name, scorer_fn in scorers.items():
        try:
            score = await scorer_fn(state, target)
            out[name] = float(score.value)
        except Exception as e:  # pragma: no cover — diagnostic only
            print(f"  [eval-mcp] {name} failed: {e}", file=sys.stderr)
            out[name] = 0.0
    return out


# ---------------------------------------------------------------------------
# DeepEval adapter — uses BedrockModel with the same judge.
# ---------------------------------------------------------------------------


def _run_deepeval(sample: dict, judge_model: str) -> dict[str, float]:
    """Run DeepEval's equivalent metrics on a single sample."""
    # Imports deferred so the script can at least print Spearman ranks
    # on cached results when DeepEval isn't installed.
    from deepeval.test_case import LLMTestCase
    from deepeval.metrics import (
        AnswerRelevancyMetric,
        ContextualPrecisionMetric,
        ContextualRecallMetric,
        ContextualRelevancyMetric,
        FaithfulnessMetric,
    )
    from deepeval.models import AmazonBedrockModel

    # DeepEval requires `region` (not region_name). It also reads
    # AWS_BEDROCK_REGION from env — set that fallback so subsequent
    # rebuilds inside the same process find a region.
    region = os.environ.get("AWS_REGION", "us-west-2")
    os.environ.setdefault("AWS_BEDROCK_REGION", region)
    model = AmazonBedrockModel(
        model_id=judge_model,
        region=region,
    )

    tc = LLMTestCase(
        input=sample["question"],
        actual_output=sample["actual_output"],
        expected_output=sample["golden_answer"],
        retrieval_context=sample["retrieval_context"],
    )

    metrics = {
        "faithfulness": FaithfulnessMetric(model=model, include_reason=False),
        "answer_relevancy": AnswerRelevancyMetric(model=model, include_reason=False),
        "contextual_precision": ContextualPrecisionMetric(model=model, include_reason=False),
        "contextual_recall": ContextualRecallMetric(model=model, include_reason=False),
        "contextual_relevancy": ContextualRelevancyMetric(model=model, include_reason=False),
    }

    out: dict[str, float] = {}
    for name, metric in metrics.items():
        try:
            metric.measure(tc)
            out[name] = float(metric.score)
        except Exception as e:  # pragma: no cover — diagnostic only
            print(f"  [deepeval] {name} failed: {e}", file=sys.stderr)
            out[name] = 0.0
    return out


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


METRICS = [
    "faithfulness",
    "answer_relevancy",
    "contextual_precision",
    "contextual_recall",
    "contextual_relevancy",
]


def _judge_model_ids() -> tuple[str, str]:
    """Return (eval_mcp_id, deepeval_id) for the same underlying Bedrock model.

    Inspect AI prefixes Bedrock models with ``bedrock/`` while DeepEval
    expects the raw model_id. ``BEDROCK_MODEL_ID`` is the raw form.
    """
    # Default to Sonnet 4.6 — the product's default judge
    # (judge_config.JUDGE_MODELS["claude"]). RAG QAG metrics are
    # single-judge (no voting), so judge quality matters; Haiku is too
    # weak for reliable verdicts. Override via BEDROCK_MODEL_ID.
    raw = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
    return (f"bedrock/{raw}", raw)


async def main() -> int:
    dataset_path = Path(__file__).with_name("reference_dataset.json")
    samples = json.loads(dataset_path.read_text())
    print(f"Loaded {len(samples)} reference samples from {dataset_path.name}")

    eval_mcp_id, deepeval_id = _judge_model_ids()
    print(f"Judge model: {deepeval_id}")
    print()

    # Cache so reruns of the print/summary step are free. Each sample
    # touches the judge ~12 times (6 ours + 6 DeepEval calls × 1-2 stages).
    cache_path = Path(__file__).with_name(".parity_cache.json")
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    all_eval_mcp: dict[str, list[float]] = {m: [] for m in METRICS}
    all_deepeval: dict[str, list[float]] = {m: [] for m in METRICS}
    sample_ids: list[str] = []

    for s in samples:
        sid = s["id"]
        sample_ids.append(sid)
        print(f"=== {sid} ===")

        # eval-mcp scoring
        em_key = f"eval_mcp:{sid}"
        if em_key in cache:
            em = cache[em_key]
            print(f"  eval-mcp (cached): {em}")
        else:
            em = await _run_eval_mcp(s, eval_mcp_id)
            cache[em_key] = em
            cache_path.write_text(json.dumps(cache, indent=2))
            print(f"  eval-mcp: {em}")

        # DeepEval scoring
        de_key = f"deepeval:{sid}"
        if de_key in cache:
            de = cache[de_key]
            print(f"  deepeval (cached): {de}")
        else:
            de = _run_deepeval(s, deepeval_id)
            cache[de_key] = de
            cache_path.write_text(json.dumps(cache, indent=2))
            print(f"  deepeval: {de}")

        for m in METRICS:
            all_eval_mcp[m].append(em.get(m, 0.0))
            all_deepeval[m].append(de.get(m, 0.0))

    # Spearman + side-by-side report
    print()
    print("=" * 70)
    print(f"{'metric':<24} {'spearman':>10}   {'eval-mcp mean':>14}  {'deepeval mean':>14}")
    print("-" * 70)
    for m in METRICS:
        em_scores = all_eval_mcp[m]
        de_scores = all_deepeval[m]
        rho = spearman(em_scores, de_scores)
        em_mean = sum(em_scores) / len(em_scores)
        de_mean = sum(de_scores) / len(de_scores)
        mark = (
            "✓" if rho >= 0.7 else ("~" if rho >= 0.5 else "✗")
        )
        print(
            f"{m:<24} {rho:>+10.3f} {mark} {em_mean:>14.3f}  {de_mean:>14.3f}"
        )
    print("=" * 70)
    print("Per-sample table (eval-mcp / deepeval):")
    print()
    header = ["sample"] + METRICS
    print(" | ".join(f"{h:<14}" for h in header))
    print("-" * (16 * len(header)))
    for i, sid in enumerate(sample_ids):
        row = [sid[:14]]
        for m in METRICS:
            row.append(f"{all_eval_mcp[m][i]:.2f}/{all_deepeval[m][i]:.2f}")
        print(" | ".join(f"{c:<14}" for c in row))

    print()
    failed = [m for m in METRICS if spearman(all_eval_mcp[m], all_deepeval[m]) < 0.5]
    if failed:
        print(f"⚠ Spearman < 0.5 on: {failed}. Investigate.")
        return 1
    print("All metrics agree on ranking (Spearman ≥ 0.5).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
