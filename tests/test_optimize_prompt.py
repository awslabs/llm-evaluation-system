"""Tests for the prompt-optimizer loop.

Layered same as test_generate_judge_iter.py:
  - Pure-function tests for the split / winner-selection / failure-pick
    / formatting helpers (no mocks, no I/O).
  - _extract_rows_from_log tests with a fake Inspect log object.
  - Loop tests with mocked _run_inspect_iteration + _run_inspect_test_ranking
    to drive the state machine through its branches without spawning
    real subprocesses.
  - One Bedrock-gated integration test that runs the real loop end-to-end
    — real subprocess, real jury, real .eval logs.

Run unit tests only:
    uv run --extra dev pytest tests/test_optimize_prompt.py -v -k "not integration"

Run everything (needs AWS creds + Bedrock model access):
    uv run --extra dev pytest tests/test_optimize_prompt.py -v
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from eval_mcp.core.judge_config import JudgeConfig
from eval_mcp.tools.optimize_prompt import (
    OPTIMIZER_TOOL,
    _extract_rows_from_log,
    _format_failures_for_optimizer,
    _format_history_for_optimizer,
    _pass_rate,
    _pick_winner,
    _safe_name_fragment,
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
# Formatting helpers — make sure improvement notes from jury_scorer's
# metadata['criteria_results'] reach the optimizer LLM prompt
# ---------------------------------------------------------------------------


def test_format_failures_includes_per_judge_improvement_notes():
    """Failed criteria should surface each judge's improvement note so
    the optimizer LLM can target what to change. The shape comes from
    jury_scorer's metadata['criteria_results']."""
    failures = [
        {
            "question": "Q1",
            "golden": "G1",
            "answer": "A1",
            "sample_score": 0.0,
            "criteria_results": [
                {
                    "name": "accuracy",
                    "votes_for": 0,
                    "total": 3,
                    "score": 0.0,
                    "improvement_notes": [
                        {"judge": "claude", "note": "Answer is wrong about X"},
                        {"judge": "nova", "note": "Missing the year"},
                    ],
                },
                {
                    "name": "structure",
                    "votes_for": 3,
                    "total": 3,
                    "score": 1.0,
                },
            ],
        }
    ]
    rendered = _format_failures_for_optimizer(failures)
    assert "Answer is wrong about X" in rendered
    assert "Missing the year" in rendered
    assert "accuracy" in rendered
    # Passing criterion should not appear in the failed list
    assert "structure" not in rendered.split("Failed criteria")[-1].split("Sample 2:")[0]


def test_format_history_truncates_long_prompts():
    history = [{"iter": 0, "prompt": "x" * 2000, "train_pass_rate": 0.5}]
    rendered = _format_history_for_optimizer(history)
    assert "truncated" in rendered.lower()
    assert len(rendered) < 2000


def test_format_history_empty():
    assert "no previous" in _format_history_for_optimizer([]).lower()


# ---------------------------------------------------------------------------
# Optimizer tool schema sanity
# ---------------------------------------------------------------------------


def test_optimizer_tool_requires_new_prompt():
    assert "new_prompt" in OPTIMIZER_TOOL["input_schema"]["required"]


def test_safe_name_fragment_strips_unsafe_chars():
    """Config names get embedded in subprocess paths, so anything that
    looks like a path traversal or shell metacharacter must get scrubbed."""
    assert _safe_name_fragment("foo/bar; rm -rf") == "foo_bar__rm_-rf"
    assert _safe_name_fragment("opt_1234") == "opt_1234"


# ---------------------------------------------------------------------------
# _extract_rows_from_log — reads what jury_scorer wrote into Score.metadata
# ---------------------------------------------------------------------------


def _fake_score(value: float, criteria_results: list[dict]):
    return SimpleNamespace(
        value=value,
        metadata={"criteria_results": criteria_results, "jury_score": value},
    )


def _fake_sample(input_: str, target: str, output: str, scores: dict):
    return SimpleNamespace(
        input=input_,
        target=target,
        output=SimpleNamespace(completion=output),
        scores=scores,
    )


def test_extract_rows_reads_jury_metadata():
    """The optimizer's only contract with jury_scorer is
    metadata['criteria_results']; this guards the read path."""
    crit = [
        {"name": "accuracy", "votes_for": 1, "total": 3, "score": 0.33,
         "improvement_notes": [{"judge": "claude", "note": "wrong year"}]},
    ]
    log = SimpleNamespace(samples=[
        _fake_sample("Q", "G", "A", {"jury_scorer": _fake_score(0.33, crit)}),
    ])
    rows = _extract_rows_from_log(log)
    assert len(rows) == 1
    assert rows[0]["sample_score"] == pytest.approx(0.33)
    assert rows[0]["question"] == "Q"
    assert rows[0]["answer"] == "A"
    assert rows[0]["criteria_results"] == crit


def test_extract_rows_handles_no_metadata():
    """Older logs or scorer variants without metadata still produce a
    valid row — sample_score lands as 0.0 if value is missing."""
    sample = SimpleNamespace(
        input="Q", target="G",
        output=SimpleNamespace(completion="A"),
        scores={"jury_scorer": SimpleNamespace(value=0.5, metadata=None)},
    )
    log = SimpleNamespace(samples=[sample])
    rows = _extract_rows_from_log(log)
    assert rows[0]["sample_score"] == 0.5
    assert rows[0]["criteria_results"] == []


def test_extract_rows_no_samples():
    assert _extract_rows_from_log(SimpleNamespace(samples=None)) == []


# ---------------------------------------------------------------------------
# Loop tests — mock the Inspect subprocess boundary, not the LLM-judge
# internals. The loop only knows about two Inspect-touching functions:
#   _run_inspect_iteration  (per-iter eval, returns (run_id, rows))
#   _run_inspect_test_ranking  (final test eval, returns (run_id, scores))
# Patching those two lets us drive every branch without touching the
# filesystem or subprocess machinery.
# ---------------------------------------------------------------------------


def _mock_rows(pass_rate: float, n: int = 5) -> list[dict]:
    """Build synthetic per-sample rows with a given mean pass rate."""
    rows = []
    n_pass = int(round(pass_rate * n))
    for i in range(n):
        score = 1.0 if i < n_pass else 0.0
        rows.append({
            "question": f"q{i}",
            "golden": f"g{i}",
            "answer": f"a{i}",
            "sample_score": score,
            "criteria_results": [
                {"name": "c", "score": score, "votes_for": int(score) * 3, "total": 3,
                 "improvement_notes": [] if score == 1.0 else [{"judge": "claude", "note": "improve x"}]}
            ],
        })
    return rows


@pytest.fixture(autouse=True)
def _isolated_user_storage(tmp_path, monkeypatch):
    """Point USER_STORAGE_BASE at a tmp dir so the loop's mkdir calls
    don't pollute the real backend/users tree."""
    monkeypatch.setenv("USER_STORAGE_BASE", str(tmp_path))
    yield


@pytest.fixture
def judge_config():
    return JudgeConfig(
        criteria=[{"name": "c", "description": "1 if good, 0 otherwise"}],
        judges={"claude": "bedrock/dummy"},
    )


@pytest.fixture
def qa_pairs():
    return _pairs(10)


def test_loop_returns_initial_on_no_train_data(judge_config):
    """0 QA pairs => degenerate train; loop returns initial as winner."""
    out = asyncio.run(
        optimize_prompt_loop(
            bedrock=None, user_id="u1", optimization_id="opt_test",
            qa_pairs=[], judge_config=judge_config, providers=["bedrock/dummy"],
            initial_prompt="{question}",
        )
    )
    assert out["winner_prompt"] == "{question}"
    assert out["status"] == "no_train_data"


def test_loop_converges_on_perfect_initial(judge_config, qa_pairs):
    """If the initial prompt scores 100% on the train sample, the loop
    exits immediately with status converged_initial — no proposer call."""
    rows_perfect = _mock_rows(1.0)

    with patch(
        "eval_mcp.tools.optimize_prompt._run_inspect_iteration",
        new=AsyncMock(return_value=("run_iter_0", rows_perfect)),
    ), patch(
        "eval_mcp.tools.optimize_prompt._run_inspect_test_ranking",
        new=AsyncMock(return_value=("run_test", {0: 1.0})),
    ), patch(
        "eval_mcp.tools.optimize_prompt._propose_new_prompt",
        new=AsyncMock(),
    ) as mock_propose:
        out = asyncio.run(
            optimize_prompt_loop(
                bedrock=None, user_id="u1", optimization_id="opt_test",
                qa_pairs=qa_pairs, judge_config=judge_config,
                providers=["bedrock/dummy"],
                initial_prompt="{question}", max_iter=3, sample_size=5,
            )
        )
    assert out["status"] == "converged_initial"
    assert out["winner_iter"] == 0
    mock_propose.assert_not_called()


def test_loop_iterates_then_converges(judge_config, qa_pairs):
    """Iter 0: 50% train. Iter 1: proposer suggests new prompt, scores 100%
    => loop converges. Winner picked by test, so mock test rates too."""
    rows_iter0 = _mock_rows(0.5)
    rows_iter1 = _mock_rows(1.0)

    async def fake_run(user_id, user_dir, log_dir, config_name, samples,
                       prompt_template, providers, judge_config):
        if prompt_template == "{question} now better":
            return f"run_{config_name}", rows_iter1
        return f"run_{config_name}", rows_iter0

    with patch(
        "eval_mcp.tools.optimize_prompt._run_inspect_iteration",
        new=AsyncMock(side_effect=fake_run),
    ), patch(
        "eval_mcp.tools.optimize_prompt._run_inspect_test_ranking",
        # Iter 1 wins on test.
        new=AsyncMock(return_value=("run_test", {0: 0.5, 1: 1.0})),
    ), patch(
        "eval_mcp.tools.optimize_prompt._propose_new_prompt",
        new=AsyncMock(return_value={"new_prompt": "{question} now better", "rationale": "fixed it"}),
    ):
        out = asyncio.run(
            optimize_prompt_loop(
                bedrock=None, user_id="u1", optimization_id="opt_test",
                qa_pairs=qa_pairs, judge_config=judge_config,
                providers=["bedrock/dummy"],
                initial_prompt="{question}", max_iter=3, sample_size=5,
            )
        )
    assert out["status"] == "converged"
    assert out["winner_prompt"] == "{question} now better"
    # Every history entry should have an eval_run_id (real linkage to Results).
    assert all(h.get("eval_run_id") for h in out["history"])
    assert out["test_run_id"] == "run_test"


def test_loop_falls_back_on_proposer_error(judge_config, qa_pairs):
    """Proposer crashes inside iter 1 => loop bails with last-good (the
    initial), status indicates the partial failure."""
    rows = _mock_rows(0.5)
    with patch(
        "eval_mcp.tools.optimize_prompt._run_inspect_iteration",
        new=AsyncMock(return_value=("run_0", rows)),
    ), patch(
        "eval_mcp.tools.optimize_prompt._run_inspect_test_ranking",
        new=AsyncMock(return_value=("run_test", {0: 0.5})),
    ), patch(
        "eval_mcp.tools.optimize_prompt._propose_new_prompt",
        new=AsyncMock(side_effect=RuntimeError("optimizer blew up")),
    ):
        out = asyncio.run(
            optimize_prompt_loop(
                bedrock=None, user_id="u1", optimization_id="opt_test",
                qa_pairs=qa_pairs, judge_config=judge_config,
                providers=["bedrock/dummy"],
                initial_prompt="{question}", max_iter=3, sample_size=5,
            )
        )
    assert out["status"].startswith("partial:")
    assert len(out["history"]) == 1
    assert out["winner_prompt"] == "{question}"


def test_loop_falls_back_on_inspect_subprocess_error(judge_config, qa_pairs):
    """Inspect subprocess fails on iter 0 (initial). Loop returns the
    initial prompt as winner with error_initial status — never crashes
    the handler."""
    with patch(
        "eval_mcp.tools.optimize_prompt._run_inspect_iteration",
        new=AsyncMock(side_effect=RuntimeError("inspect eval exited 1")),
    ), patch(
        "eval_mcp.tools.optimize_prompt._run_inspect_test_ranking",
        new=AsyncMock(),
    ):
        out = asyncio.run(
            optimize_prompt_loop(
                bedrock=None, user_id="u1", optimization_id="opt_test",
                qa_pairs=qa_pairs, judge_config=judge_config,
                providers=["bedrock/dummy"],
                initial_prompt="{question}", max_iter=3, sample_size=5,
            )
        )
    assert out["status"].startswith("error_initial")
    assert out["winner_prompt"] == "{question}"
    assert out["winner_iter"] == 0


def test_loop_respects_max_iter(judge_config, qa_pairs):
    """Proposer always proposes; the loop exits at max_iter without
    surfacing a 'didn't converge' flag (status stays 'complete')."""
    rows = _mock_rows(0.5)
    counter = {"n": 0}

    async def proposer(bedrock, current, failures, history):
        counter["n"] += 1
        return {"new_prompt": f"{{question}} v{counter['n']}", "rationale": "..."}

    with patch(
        "eval_mcp.tools.optimize_prompt._run_inspect_iteration",
        new=AsyncMock(return_value=("run_x", rows)),
    ), patch(
        "eval_mcp.tools.optimize_prompt._run_inspect_test_ranking",
        new=AsyncMock(return_value=("run_test", {0: 0.5, 1: 0.5, 2: 0.5})),
    ), patch(
        "eval_mcp.tools.optimize_prompt._propose_new_prompt",
        new=AsyncMock(side_effect=proposer),
    ):
        out = asyncio.run(
            optimize_prompt_loop(
                bedrock=None, user_id="u1", optimization_id="opt_test",
                qa_pairs=qa_pairs, judge_config=judge_config,
                providers=["bedrock/dummy"],
                initial_prompt="{question}", max_iter=2, sample_size=5,
            )
        )
    # Iter 0 (initial) + 2 proposer-driven iters = 3 entries.
    assert len(out["history"]) == 3
    assert out["status"] == "complete"


def test_loop_test_scores_populated(judge_config, qa_pairs):
    """Every history iter should have an entry in test_scores_by_iter."""
    rows = _mock_rows(0.5)
    with patch(
        "eval_mcp.tools.optimize_prompt._run_inspect_iteration",
        new=AsyncMock(return_value=("run_x", rows)),
    ), patch(
        "eval_mcp.tools.optimize_prompt._run_inspect_test_ranking",
        new=AsyncMock(return_value=("run_test", {0: 0.5, 1: 0.6})),
    ), patch(
        "eval_mcp.tools.optimize_prompt._propose_new_prompt",
        new=AsyncMock(return_value={"new_prompt": "{question} v1", "rationale": "..."}),
    ):
        out = asyncio.run(
            optimize_prompt_loop(
                bedrock=None, user_id="u1", optimization_id="opt_test",
                qa_pairs=qa_pairs, judge_config=judge_config,
                providers=["bedrock/dummy"],
                initial_prompt="{question}", max_iter=1, sample_size=5,
            )
        )
    iters_in_history = {h["iter"] for h in out["history"]}
    iters_in_test_scores = set(out["test_scores_by_iter"].keys())
    assert iters_in_history == iters_in_test_scores


def test_loop_test_ranking_failure_falls_back_to_train_scores(judge_config, qa_pairs):
    """If the test-ranking subprocess fails, the loop must still return a
    winner — fall back to using train pass rates instead of crashing the
    whole optimization."""
    rows = _mock_rows(0.5)
    with patch(
        "eval_mcp.tools.optimize_prompt._run_inspect_iteration",
        new=AsyncMock(return_value=("run_x", rows)),
    ), patch(
        "eval_mcp.tools.optimize_prompt._run_inspect_test_ranking",
        new=AsyncMock(side_effect=RuntimeError("test eval crashed")),
    ), patch(
        "eval_mcp.tools.optimize_prompt._propose_new_prompt",
        new=AsyncMock(return_value={"new_prompt": "{question} v1", "rationale": "..."}),
    ):
        out = asyncio.run(
            optimize_prompt_loop(
                bedrock=None, user_id="u1", optimization_id="opt_test",
                qa_pairs=qa_pairs, judge_config=judge_config,
                providers=["bedrock/dummy"],
                initial_prompt="{question}", max_iter=1, sample_size=5,
            )
        )
    # All history iters have train scores so the winner picker has data.
    assert out["winner_iter"] in {h["iter"] for h in out["history"]}
    assert out["test_run_id"] is None


# ---------------------------------------------------------------------------
# Integration test (Bedrock-gated): real Inspect subprocess, real jury
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
def test_integration_optimizer_does_not_regress(tmp_path, monkeypatch):
    """End-to-end: run the loop against 5 mixed QA pairs with a
    deliberately weak initial prompt. Spawns real Inspect subprocesses
    with the real jury. The winner's test score must NOT be lower than
    the initial's test score — even if the loop can't improve, it must
    not make things worse."""
    monkeypatch.setenv("USER_STORAGE_BASE", str(tmp_path))
    from eval_mcp.core.bedrock_client import BedrockClient

    bedrock = BedrockClient()
    judge_config = JudgeConfig(
        criteria=[
            {"name": "factual_accuracy", "description": "1 if facts are correct, 0 otherwise"},
            {"name": "directness", "description": "1 if answers the question directly, 0 otherwise"},
            {"name": "specificity", "description": "1 if includes specific details, 0 otherwise"},
        ],
        # Use a single judge in CI to keep wall time and Bedrock spend
        # manageable. The production loop uses the full 3-judge jury;
        # this test just needs to verify the Inspect plumbing works end-
        # to-end and the loop doesn't regress.
        judges={"claude": "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"},
    )
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
            user_id="integration_test",
            optimization_id="opt_itest",
            qa_pairs=qa_pairs,
            judge_config=judge_config,
            providers=["bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"],
            initial_prompt="{question}",
            max_iter=2,
            sample_size=3,
            test_holdout=0.4,
        )
    )

    print(f"\nStatus: {out['status']}")
    print(f"Winner iter: {out['winner_iter']}  Test score: {out['winner_test_score']:.2f}")
    print(f"History pass rates (train): {[(h['iter'], round(h['train_pass_rate'], 2), h.get('eval_run_id')) for h in out['history']]}")
    print(f"Test scores: {out['test_scores_by_iter']}")
    print(f"Test run_id: {out.get('test_run_id')}")
    print(f"Winner prompt:\n{out['winner_prompt']}\n")

    # Every iteration must have produced a real run_id — that's the
    # whole point of routing through Inspect.
    assert all(h.get("eval_run_id") for h in out["history"]), \
        "Iterations are missing eval_run_id — Inspect routing is broken"

    initial_test = out["test_scores_by_iter"].get(0, 0.0)
    assert out["winner_test_score"] >= initial_test - 0.001, (
        f"Winner regressed below initial: {out['winner_test_score']} < {initial_test}"
    )
