"""Tests for the prompt-optimizer loop.

Layered same as test_generate_judge_iter.py:
  - Pure-function tests for the split / winner-selection / failure-pick
    helpers (no mocks).
  - Loop tests with mocked _eval_samples + _propose_new_prompt to drive
    the state machine through its branches.
  - One Bedrock-gated integration test that runs the real loop end-to-end
    against a 5-sample fixture.

Run unit tests only:
    uv run --extra dev pytest tests/test_optimize_prompt.py -v -k "not integration"

Run everything (needs AWS creds + Bedrock model access):
    uv run --extra dev pytest tests/test_optimize_prompt.py -v
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

from eval_mcp.tools.optimize_prompt import (
    OPTIMIZER_TOOL,
    _pass_rate,
    _pick_winner,
    _select_failures,
    _split_train_test,
    optimize_prompt_loop,
)


# ---------------------------------------------------------------------------
# _split_train_test — pure, deterministic given seed
# ---------------------------------------------------------------------------


def _pairs(n: int) -> list[dict]:
    return [{"question": f"q{i}", "golden_answer": f"a{i}"} for i in range(n)]


def test_split_zero_pairs_returns_empty():
    train, test = _split_train_test([])
    assert train == []
    assert test == []


def test_split_one_pair_degenerate_both_sides():
    train, test = _split_train_test(_pairs(1))
    assert train == test == _pairs(1)


def test_split_deterministic_with_seed():
    pairs = _pairs(20)
    t1, te1 = _split_train_test(pairs, holdout=0.4, seed=42)
    t2, te2 = _split_train_test(pairs, holdout=0.4, seed=42)
    assert t1 == t2
    assert te1 == te2


def test_split_different_seeds_diverge():
    pairs = _pairs(20)
    t1, _ = _split_train_test(pairs, holdout=0.4, seed=1)
    t2, _ = _split_train_test(pairs, holdout=0.4, seed=2)
    assert t1 != t2


def test_split_test_size_floor_one():
    """Even with tiny holdout fractions, test gets at least 1 sample so
    we can rank prompts at the end."""
    train, test = _split_train_test(_pairs(10), holdout=0.01)
    assert len(test) >= 1
    assert len(train) >= 1


def test_split_test_size_does_not_consume_everything():
    """holdout=1.0 would zero out train; loop needs at least 1 train."""
    train, test = _split_train_test(_pairs(5), holdout=1.0)
    assert len(train) >= 1
    assert len(test) >= 1


# ---------------------------------------------------------------------------
# _pick_winner
# ---------------------------------------------------------------------------


def test_pick_winner_highest_test_score():
    history = [
        {"iter": 0, "prompt": "p0"},
        {"iter": 1, "prompt": "p1"},
        {"iter": 2, "prompt": "p2"},
    ]
    scores = {0: 0.3, 1: 0.9, 2: 0.5}
    iter_, prompt, score = _pick_winner(history, scores)
    assert iter_ == 1
    assert prompt == "p1"
    assert score == 0.9


def test_pick_winner_ties_broken_by_earlier_iter():
    """When two iters tie on test score the earlier one wins — simpler
    prompts are preferable when equally good."""
    history = [
        {"iter": 0, "prompt": "p0"},
        {"iter": 1, "prompt": "p1"},
        {"iter": 2, "prompt": "p2"},
    ]
    scores = {0: 0.5, 1: 0.5, 2: 0.5}
    iter_, prompt, score = _pick_winner(history, scores)
    assert iter_ == 0


def test_pick_winner_falls_back_when_no_scores():
    """Empty test set or all-NaN test scores: winner defaults to iter 0
    (initial prompt) so the caller never sees a missing winner."""
    history = [{"iter": 0, "prompt": "p0"}]
    iter_, prompt, score = _pick_winner(history, {})
    assert iter_ == 0
    assert prompt == "p0"


# ---------------------------------------------------------------------------
# _select_failures + _pass_rate
# ---------------------------------------------------------------------------


def test_select_failures_returns_lowest_sorted():
    rows = [
        {"sample_score": 1.0},
        {"sample_score": 0.5},
        {"sample_score": 0.0},
        {"sample_score": 0.75},
    ]
    failures = _select_failures(rows, max_n=10)
    assert len(failures) == 3  # the 1.0 is excluded
    assert [f["sample_score"] for f in failures] == [0.0, 0.5, 0.75]


def test_select_failures_caps_at_max_n():
    rows = [{"sample_score": i / 10} for i in range(20)]
    failures = _select_failures(rows, max_n=5)
    assert len(failures) == 5


def test_select_failures_empty_when_all_pass():
    rows = [{"sample_score": 1.0} for _ in range(5)]
    assert _select_failures(rows) == []


def test_pass_rate_simple():
    rows = [{"sample_score": 1.0}, {"sample_score": 0.0}, {"sample_score": 0.5}]
    assert _pass_rate(rows) == pytest.approx(0.5)


def test_pass_rate_empty_zero():
    assert _pass_rate([]) == 0.0


# ---------------------------------------------------------------------------
# Optimizer tool schema sanity
# ---------------------------------------------------------------------------


def test_optimizer_tool_requires_new_prompt():
    assert "new_prompt" in OPTIMIZER_TOOL["input_schema"]["required"]


# ---------------------------------------------------------------------------
# Loop tests (mocked LLM)
# ---------------------------------------------------------------------------


def _mock_eval_rows(pass_rate: float, n: int = 5) -> list[dict]:
    """Build a synthetic eval result with a given mean pass rate."""
    rows = []
    n_pass = int(round(pass_rate * n))
    for i in range(n):
        score = 1.0 if i < n_pass else 0.0
        rows.append({
            "question": f"q{i}",
            "golden": f"g{i}",
            "answer": f"a{i}",
            "sample_score": score,
            "criteria": [{"name": "c", "score": int(score), "improvement_note": "x"}],
        })
    return rows


@pytest.fixture
def criteria():
    return [{"name": "c", "description": "1 if good, 0 otherwise"}]


@pytest.fixture
def qa_pairs():
    return _pairs(10)


def test_loop_returns_initial_on_no_train_data(criteria):
    """0 QA pairs => degenerate train; loop returns initial as winner."""
    out = asyncio.run(
        optimize_prompt_loop(
            bedrock=None, qa_pairs=[], criteria=criteria,
            initial_prompt="{question}",
        )
    )
    assert out["winner_prompt"] == "{question}"
    assert out["status"] == "no_train_data"


def test_loop_converges_on_perfect_initial(criteria, qa_pairs):
    """If the initial prompt scores 100% on the train sample, the loop
    exits immediately with status converged_initial — no proposer call."""
    rows_perfect = _mock_eval_rows(1.0)
    with patch(
        "eval_mcp.tools.optimize_prompt._eval_samples",
        new=AsyncMock(return_value=rows_perfect),
    ), patch(
        "eval_mcp.tools.optimize_prompt._propose_new_prompt",
        new=AsyncMock(),
    ) as mock_propose:
        out = asyncio.run(
            optimize_prompt_loop(
                bedrock=None, qa_pairs=qa_pairs, criteria=criteria,
                initial_prompt="{question}", max_iter=3, sample_size=5,
            )
        )
    assert out["status"] == "converged_initial"
    assert out["winner_iter"] == 0
    mock_propose.assert_not_called()


def test_loop_iterates_then_converges(criteria, qa_pairs):
    """Iter 0: 50% train. Iter 1: proposer suggests new prompt, scores 100%
    => loop converges. Winner picked by test, so we mock test rates too."""
    rows_iter0 = _mock_eval_rows(0.5)
    rows_iter1 = _mock_eval_rows(1.0)

    async def fake_eval(bedrock, prompt, criteria, samples):
        # Distinguish iterations by what prompt is being evaluated.
        return rows_iter1 if prompt == "{question} now better" else rows_iter0

    with patch(
        "eval_mcp.tools.optimize_prompt._eval_samples",
        new=AsyncMock(side_effect=fake_eval),
    ), patch(
        "eval_mcp.tools.optimize_prompt._propose_new_prompt",
        new=AsyncMock(return_value={"new_prompt": "{question} now better", "rationale": "fixed it"}),
    ):
        out = asyncio.run(
            optimize_prompt_loop(
                bedrock=None, qa_pairs=qa_pairs, criteria=criteria,
                initial_prompt="{question}", max_iter=3, sample_size=5,
            )
        )
    assert out["status"] == "converged"
    # Winner should be the iter-1 prompt since it had higher test score.
    assert out["winner_prompt"] == "{question} now better"


def test_loop_falls_back_on_proposer_error(criteria, qa_pairs):
    """Proposer crashes inside iter 1 => loop bails with last-good (the
    initial), status indicates the partial failure."""
    rows = _mock_eval_rows(0.5)
    with patch(
        "eval_mcp.tools.optimize_prompt._eval_samples",
        new=AsyncMock(return_value=rows),
    ), patch(
        "eval_mcp.tools.optimize_prompt._propose_new_prompt",
        new=AsyncMock(side_effect=RuntimeError("optimizer blew up")),
    ):
        out = asyncio.run(
            optimize_prompt_loop(
                bedrock=None, qa_pairs=qa_pairs, criteria=criteria,
                initial_prompt="{question}", max_iter=3, sample_size=5,
            )
        )
    assert out["status"].startswith("partial:")
    # Only iter 0 in history.
    assert len(out["history"]) == 1
    assert out["winner_prompt"] == "{question}"


def test_loop_respects_max_iter(criteria, qa_pairs):
    """Proposer always proposes; the loop exits at max_iter without
    surfacing a 'didn't converge' flag (status stays 'complete')."""
    rows = _mock_eval_rows(0.5)
    counter = {"n": 0}

    async def proposer(bedrock, current, failures, history):
        counter["n"] += 1
        return {"new_prompt": f"{{question}} v{counter['n']}", "rationale": "..."}

    with patch(
        "eval_mcp.tools.optimize_prompt._eval_samples",
        new=AsyncMock(return_value=rows),
    ), patch(
        "eval_mcp.tools.optimize_prompt._propose_new_prompt",
        new=AsyncMock(side_effect=proposer),
    ):
        out = asyncio.run(
            optimize_prompt_loop(
                bedrock=None, qa_pairs=qa_pairs, criteria=criteria,
                initial_prompt="{question}", max_iter=2, sample_size=5,
            )
        )
    # Iter 0 (initial) + 2 proposer-driven iters = 3 entries.
    assert len(out["history"]) == 3
    assert out["status"] == "complete"


def test_loop_test_scores_populated(criteria, qa_pairs):
    """Every history iter should have an entry in test_scores_by_iter."""
    rows = _mock_eval_rows(0.5)
    with patch(
        "eval_mcp.tools.optimize_prompt._eval_samples",
        new=AsyncMock(return_value=rows),
    ), patch(
        "eval_mcp.tools.optimize_prompt._propose_new_prompt",
        new=AsyncMock(return_value={"new_prompt": "{question} v1", "rationale": "..."}),
    ):
        out = asyncio.run(
            optimize_prompt_loop(
                bedrock=None, qa_pairs=qa_pairs, criteria=criteria,
                initial_prompt="{question}", max_iter=1, sample_size=5,
            )
        )
    iters_in_history = {h["iter"] for h in out["history"]}
    iters_in_test_scores = set(out["test_scores_by_iter"].keys())
    assert iters_in_history == iters_in_test_scores


# ---------------------------------------------------------------------------
# Integration test (Bedrock-gated)
# ---------------------------------------------------------------------------


def _bedrock_reachable() -> bool:
    try:
        import boto3

        session = boto3.Session(region_name=os.environ.get("AWS_REGION", "us-west-2"))
        creds = session.get_credentials()
        return creds is not None and creds.access_key is not None
    except Exception:
        return False


@pytest.mark.skipif(
    not _bedrock_reachable(),
    reason="Bedrock credentials not available — skipping live optimizer integration test.",
)
def test_integration_optimizer_does_not_regress():
    """End-to-end: run the loop against 5 mixed QA pairs with a
    deliberately weak initial prompt. The winner's test score must NOT
    be lower than the initial's test score — even if the loop can't
    improve, it should at least not make things worse."""
    from eval_mcp.core.bedrock_client import BedrockClient

    bedrock = BedrockClient()
    criteria = [
        {"name": "factual_accuracy", "description": "1 if facts are correct, 0 otherwise"},
        {"name": "directness", "description": "1 if answers the question directly, 0 otherwise"},
        {"name": "specificity", "description": "1 if includes specific details, 0 otherwise"},
    ]
    qa_pairs = [
        {"question": "What's the capital of France?",
         "golden_answer": "Paris is the capital of France."},
        {"question": "Who wrote Hamlet?",
         "golden_answer": "William Shakespeare wrote Hamlet around 1600."},
        {"question": "Explain photosynthesis briefly.",
         "golden_answer": "Plants use chlorophyll to convert sunlight, water, and CO2 into glucose and oxygen."},
        {"question": "What is the Pythagorean theorem?",
         "golden_answer": "For a right triangle, a² + b² = c² where c is the hypotenuse."},
        {"question": "What causes the seasons?",
         "golden_answer": "Earth's axial tilt of ~23.5° relative to its orbit changes which hemisphere gets more direct sunlight."},
    ]

    out = asyncio.run(
        optimize_prompt_loop(
            bedrock=bedrock,
            qa_pairs=qa_pairs,
            criteria=criteria,
            initial_prompt="{question}",
            max_iter=2,
            sample_size=3,
            test_holdout=0.4,
        )
    )

    print(f"\nStatus: {out['status']}")
    print(f"Winner iter: {out['winner_iter']}  Test score: {out['winner_test_score']:.2f}")
    print(f"History pass rates (train): {[(h['iter'], round(h['train_pass_rate'], 2)) for h in out['history']]}")
    print(f"Test scores: {out['test_scores_by_iter']}")
    print(f"Winner prompt:\n{out['winner_prompt']}\n")

    initial_test = out["test_scores_by_iter"].get(0, 0.0)
    assert out["winner_test_score"] >= initial_test - 0.001, (
        f"Winner regressed below initial: {out['winner_test_score']} < {initial_test}"
    )
