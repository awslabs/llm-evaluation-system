"""Tests for the shared jury kernel (eval_mcp/core/jury.py).

The kernel owns the mechanics every jury shares: judge-list normalization,
fan-out with failure exclusion, and the two tallies. The judge-selection cases
(defaults, comma scalar, dedup) are covered in test_aiwf_benchmark.py where
they were written against the original implementation; this file pins the
fan-out and tally semantics.
"""

import pytest

from eval_mcp.core.jury import collect_verdicts, fraction, majority_passes


@pytest.mark.asyncio
async def test_collect_verdicts_splits_successes_from_failures():
    async def call(label, model_id):
        if label == "bad":
            raise RuntimeError("throttled")
        if label == "mute":
            return None, "no tool call"
        return {"ok": model_id}, None

    verdicts, errors = await collect_verdicts(
        {"good": "m1", "bad": "m2", "mute": "m3"}, call
    )
    assert verdicts == {"good": {"ok": "m1"}}
    assert errors["bad"] == ("exception", "throttled")
    assert errors["mute"] == ("unparseable", "no tool call")


@pytest.mark.asyncio
async def test_collect_verdicts_parallel_matches_sequential():
    async def call(label, model_id):
        return label.upper(), None

    judges = {"a": "m1", "b": "m2"}
    seq = await collect_verdicts(judges, call, parallel=False)
    par = await collect_verdicts(judges, call, parallel=True)
    assert seq == par == ({"a": "A", "b": "B"}, {})


@pytest.mark.asyncio
async def test_all_judges_failing_yields_empty_verdicts_not_a_raise():
    async def call(label, model_id):
        raise RuntimeError("down")

    verdicts, errors = await collect_verdicts({"j1": "m1", "j2": "m2"}, call)
    assert verdicts == {}
    assert set(errors) == {"j1", "j2"}


def test_majority_is_strict_ties_fail():
    assert majority_passes(1, 1)          # solo judge: identity
    assert majority_passes(2, 3)
    assert not majority_passes(1, 3)
    assert not majority_passes(1, 2)      # even-jury tie fails
    assert not majority_passes(0, 0)      # nobody voted: fail, not crash


def test_fraction_tally():
    assert fraction([1, 1, 0]) == pytest.approx(2 / 3)
    assert fraction([]) == 0.0
