"""Tests for the catastrophic-failure detection in run_evaluation.

Background: Inspect AI runs with `--no-fail-on-error`, which means even when
every sample raises, the process exits 0 and the .eval log is "complete."
Our wrapper used to read that log, see no scores, and return
`{"success": true, "scores": []}` to the caller. That hid real bugs:
the OTel sitecustomize grandchild-leak shipped with green status because
nobody noticed the empty scores.

`is_catastrophic_eval_failure` is the predicate that flips success=false
when the eval ran but produced no scores. These tests pin its behavior so
we don't accidentally re-introduce silent failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from eval_mcp.tools.run_eval import is_catastrophic_eval_failure


@dataclass
class _FakeResults:
    """Stand-in for inspect_ai.log.results — only the two fields we read."""
    total_samples: int = 0
    completed_samples: int = 0


def test_real_scores_are_not_catastrophic():
    """A run with any scorer output is a real eval — even 0% accuracy.

    The point of fail-loud is to distinguish 'real bad scores' from 'the
    capture pipeline broke.' Real bad scores are still success.
    """
    scores = [{"scorer": "accuracy", "metrics": {"accuracy": 0.0}}]
    results = _FakeResults(total_samples=10, completed_samples=10)
    assert is_catastrophic_eval_failure(scores, results) is False


def test_no_scores_with_no_results_is_catastrophic():
    """Task crashed during setup (e.g. dataset failed to load, config bug).

    log.results is None because Inspect never got far enough to populate it.
    Definitely catastrophic — not a real eval.
    """
    assert is_catastrophic_eval_failure([], None) is True


def test_no_scores_but_all_samples_completed_is_not_catastrophic():
    """Defensive case: scores=[] but every sample completed.

    Shouldn't happen in practice (if samples completed, scorers ran), but if
    we ever change the scorer pipeline this guards against false positives.
    The signal we care about is 'samples errored,' not 'scores empty.'
    """
    results = _FakeResults(total_samples=10, completed_samples=10)
    assert is_catastrophic_eval_failure([], results) is False


def test_no_scores_and_zero_completed_is_catastrophic():
    """The exact bug we hit: 5 samples planned, every one errored.

    log.results exists (Inspect populated it) but completed_samples is 0
    because every sample raised before scoring. This is the OTel-leak
    fingerprint: green log, empty scores, broken capture.
    """
    results = _FakeResults(total_samples=5, completed_samples=0)
    assert is_catastrophic_eval_failure([], results) is True


def test_partial_completion_with_scores_is_not_catastrophic():
    """3 of 5 samples errored but 2 produced scores.

    User probably wants to see the partial result and decide. Not
    catastrophic — fail-loud only fires when everything broke.
    """
    scores = [{"scorer": "accuracy", "metrics": {"accuracy": 0.5}}]
    results = _FakeResults(total_samples=5, completed_samples=2)
    assert is_catastrophic_eval_failure(scores, results) is False


def test_zero_total_samples_with_no_scores_is_not_catastrophic():
    """Edge case: empty dataset.

    total_samples=0 means nothing was supposed to run. Empty scores is the
    correct outcome, not a failure. Don't fire fail-loud on this.
    """
    results = _FakeResults(total_samples=0, completed_samples=0)
    assert is_catastrophic_eval_failure([], results) is False


def test_results_object_without_expected_attrs_does_not_crash():
    """Forward-compat: if Inspect's results schema gains/loses fields, the
    predicate must not blow up. Defensive getattr access.
    """
    class _Bare:
        pass
    # Has neither total_samples nor completed_samples — getattr defaults
    # to 0, so total_samples (0) > 0 is False → not catastrophic.
    assert is_catastrophic_eval_failure([], _Bare()) is False
