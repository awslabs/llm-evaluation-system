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

import json
from unittest.mock import patch

from eval_mcp.tools import external_providers as ep
from eval_mcp.tools.run_eval import (
    _PROVIDER_PATTERN,
    _mantle_region_hint,
    _summarize_failure,
    is_catastrophic_eval_failure,
)


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


# ---------------------------------------------------------------------------
# _summarize_failure — recovering the cause from Inspect's output
#
# Companion bug to the above: when the subprocess exits non-zero we used to read
# only stderr, but Inspect's CLI renders fatal errors through its Rich console
# on **stdout**. The result was the least useful report possible —
# `success: false, exit code 1, stderr: ""` — with the real cause unread.
#
# The strings below are verbatim captures from real failing `inspect eval` runs
# (2026-07-27, inspect_ai 0.3.241), trimmed for length. Box-drawing characters
# and line wrapping are preserved because that framing is exactly what the
# parser has to cope with.
# ---------------------------------------------------------------------------


# Missing task file. stdout only; stderr was literally 0 bytes.
_STDOUT_NO_TASKS = "\nError: No inspect tasks were found at the specified paths.\n"

# Credential failure mid-run, rendered as a Rich traceback panel on stdout.
# Note the exit code was 0 here — a different failure mode, but the same
# "the cause is in stdout" property.
_STDOUT_RICH_TRACEBACK = """\
╭──────────────────────────────────────────────────────────────────────────────╮
│eval_task (1 sample): openai/bedrock/gpt-5.4                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭───────────────────── Traceback (most recent call last) ──────────────────────╮
│ /path/to/site-packages/openai/_base_client.py                                │
│ in _refresh_api_key                                                          │
╰──────────────────────────────────────────────────────────────────────────────╯
CredentialRetrievalError: Error when retrieving credentials from custom-process:
Task interrupted (no samples completed before interruption)
"""

# An ImportError in the task file DOES go to stderr, wrapped in ASCII box
# framing (Rich falls back to `|` when it can't use Unicode).
_STDERR_ASCII_FRAMED = """\
| /path/to/configs/broken.py:2 in <module>                                     |
| ModuleNotFoundError: No module named 'nonexistent_module_xyz'                |
"""


def test_summarize_reads_stdout_when_stderr_is_empty():
    """The core regression: stderr empty, cause sitting in stdout.

    This is the exact shape of the bug — an eval that failed with an unhelpful
    `stderr: ""` while stdout said precisely what was wrong.
    """
    assert (
        _summarize_failure("", _STDOUT_NO_TASKS)
        == "Error: No inspect tasks were found at the specified paths."
    )


def test_summarize_extracts_exception_from_rich_traceback_panel():
    """Pull the exception line out of Inspect's box-drawn traceback."""
    result = _summarize_failure("", _STDOUT_RICH_TRACEBACK)
    assert result.startswith("CredentialRetrievalError:")
    # Box-drawing characters must not survive into the message.
    assert "│" not in result


def test_summarize_strips_ascii_box_framing():
    """Rich uses `|` framing on terminals without Unicode; strip it too.

    Without this the agent sees "| ModuleNotFoundError: ..." — readable, but
    it leaks our rendering details into what should be a clean error string.
    """
    result = _summarize_failure(_STDERR_ASCII_FRAMED, "")
    assert result == "ModuleNotFoundError: No module named 'nonexistent_module_xyz'"


def test_summarize_prefers_stderr_over_stdout():
    """A genuine crash on stderr outranks whatever stdout was mid-render.

    stdout during a failed run is mostly progress UI; if stderr has content
    it's the more direct signal.
    """
    result = _summarize_failure(_STDERR_ASCII_FRAMED, _STDOUT_NO_TASKS)
    assert result.startswith("ModuleNotFoundError")


def test_summarize_returns_empty_when_nothing_recognizable():
    """No marker match → "" so the caller keeps its generic exit-code message.

    Inventing a summary from unrecognized output would be worse than saying
    nothing: it would bury the exit code behind a misleading line.
    """
    assert _summarize_failure("", "eval_task (5 samples): all good\nmean 0.6\n") == ""


def test_summarize_handles_both_streams_empty():
    """Process died with no output at all (e.g. SIGKILL / OOM)."""
    assert _summarize_failure("", "") == ""


# ---------------------------------------------------------------------------
# _mantle_region_hint — "not available" must not mean "doesn't exist"
#
# OpenAI's frontier models launch region-by-region on Bedrock. gpt-5.5 and
# gpt-5.6-sol were us-east-1/us-east-2 only while gpt-5.6-terra/luna and
# gpt-5.4 were also in us-west-2. A bare "model not available" reads as "this
# model doesn't exist" — which is precisely the wrong conclusion to invite.
# ---------------------------------------------------------------------------


def test_region_hint_names_regions_that_serve_the_model():
    with patch.object(
        ep, "find_mantle_regions_for_model", lambda _m: ["us-east-1", "us-east-2"]
    ):
        hint = _mantle_region_hint("openai/bedrock/gpt-5.6-sol", "us-west-2")

    assert "us-west-2" in hint  # where we looked
    assert "us-east-1" in hint and "us-east-2" in hint  # where it lives
    assert "AWS_REGION=us-east-1" in hint  # the actionable fix


def test_region_hint_excludes_the_region_we_already_tried():
    """Suggesting the region that just failed would be nonsense."""
    with patch.object(
        ep, "find_mantle_regions_for_model", lambda _m: ["us-east-2", "us-west-2"]
    ):
        hint = _mantle_region_hint("openai/bedrock/gpt-5.5", "us-west-2")

    assert "AWS_REGION=us-east-2" in hint
    assert "AWS_REGION=us-west-2" not in hint


def test_region_hint_when_model_found_nowhere():
    """Genuinely unknown model — don't imply a region change will help."""
    with patch.object(ep, "find_mantle_regions_for_model", lambda _m: []):
        hint = _mantle_region_hint("openai/bedrock/gpt-9.9-nope", "us-west-2")

    assert "wasn't found in any other region" in hint
    assert "AWS_REGION=" not in hint


def test_provider_pattern_matches_both_bedrock_endpoints():
    """Mantle model IDs must be extracted for pre-flight validation.

    The old pattern was `"(bedrock/[^"]+)"`, whose leading `"` anchor made
    "openai/bedrock/gpt-5.5" unmatchable. Every GPT-5.x config therefore skipped
    validation entirely and failed later with an opaque non-zero exit instead of
    the actionable region/access message.
    """
    config = json.dumps(
        {
            "providers": [
                "openai/bedrock/gpt-5.6-sol",
                "bedrock/us.anthropic.claude-sonnet-4-6",
            ],
            "judge_models": {"openai/bedrock/gpt-5.5": "openai/bedrock/gpt-5.5"},
        }
    )
    found = set(_PROVIDER_PATTERN.findall(config))
    assert "openai/bedrock/gpt-5.6-sol" in found
    assert "openai/bedrock/gpt-5.5" in found
    assert "bedrock/us.anthropic.claude-sonnet-4-6" in found


def test_provider_pattern_does_not_match_unrelated_strings():
    """Don't sweep up direct-API or non-Bedrock IDs — they need no smoke test."""
    config = json.dumps(
        {"providers": ["openai/gpt-5.4", "anthropic/claude-opus-4-6", "google/gemini-3-flash"]}
    )
    assert _PROVIDER_PATTERN.findall(config) == []


def test_region_hint_survives_probe_failure():
    """A failed probe must degrade to the plain message, not break validation.

    This runs inside the error path of _validate_providers — an exception here
    would replace an actionable model error with an unrelated stack trace.
    """

    def _boom(_m):
        raise RuntimeError("network down")

    with patch.object(ep, "find_mantle_regions_for_model", _boom):
        hint = _mantle_region_hint("openai/bedrock/gpt-5.5", "us-west-2")

    assert "us-west-2" in hint
    assert "AWS_REGION=" not in hint


# ---------------------------------------------------------------------------
# max_tokens default
# ---------------------------------------------------------------------------


def test_converse_run_raises_max_tokens_above_inspect_default():
    """A Converse run must pass a ceiling above Inspect's 2048.

    Inspect's Bedrock provider defaults to 2048, which reasoning models can
    consume entirely on their reasoning channel — producing an empty completion
    that scores 0 while the run reports success. Measured: gpt-5.6-luna and
    gpt-5.6-sol both hit 2048/2048 reasoning tokens with no visible output.

    The value is now resolved per model from its advertised maximum rather than
    a single constant (see _max_tokens_for_run), but the invariant that matters
    to this failure — a Converse run never inherits the 2048 default — still
    holds and is what this guards.
    """
    from eval_mcp.tools.run_eval import _MAX_TOKENS_FALLBACK, _max_tokens_for_run

    assert _MAX_TOKENS_FALLBACK > 2048, "fallback must exceed Inspect's default"
    # A known Converse model resolves to its real limit, which is far above 2048.
    resolved = _max_tokens_for_run(["bedrock/us.amazon.nova-pro-v1:0"])
    assert resolved is not None and resolved > 2048


# ---------------------------------------------------------------------------
# Cross-region routing for Mantle models
#
# Mantle availability is per-region but AWS credentials are global, so a user in
# us-west-2 or eu-west-1 CAN invoke a us-east-only model by pointing that request
# at us-east-2. Without this the MCP only worked for users who happened to be in
# a us-east region and told everyone else the model did not exist.
# ---------------------------------------------------------------------------


def test_region_for_run_prefers_a_region_serving_the_models():
    from eval_mcp.tools.run_eval import _region_for_run

    cfg = {"mantle_regions": {"openai/bedrock/gpt-5.6-sol": "us-east-2"}}
    models = ["bedrock/us.anthropic.claude-haiku-4-5", "openai/bedrock/gpt-5.6-sol"]
    assert _region_for_run(cfg, models) == "us-east-2"


def test_region_for_run_falls_back_to_ambient_when_no_override_needed():
    """No routing when the caller's own region already serves everything."""
    from eval_mcp.core.bedrock_client import resolve_region
    from eval_mcp.tools.run_eval import _region_for_run

    assert _region_for_run({}, ["bedrock/us.anthropic.claude-haiku-4-5"]) == resolve_region()


def test_region_for_run_ignores_overrides_for_models_not_in_this_run():
    """A stale entry for an unused model must not relocate the run."""
    from eval_mcp.core.bedrock_client import resolve_region
    from eval_mcp.tools.run_eval import _region_for_run

    cfg = {"mantle_regions": {"openai/bedrock/gpt-5.5": "us-east-1"}}
    assert _region_for_run(cfg, ["bedrock/amazon.nova-pro-v1:0"]) == resolve_region()


def test_region_for_run_is_deterministic_with_conflicting_regions():
    """Two models wanting different regions must resolve the same way every time.

    Non-deterministic selection would make a config's behaviour depend on dict
    iteration order — the kind of flake that is nearly impossible to diagnose.
    """
    from eval_mcp.tools.run_eval import _region_for_run

    cfg = {
        "mantle_regions": {
            "openai/bedrock/gpt-5.5": "us-east-2",
            "openai/bedrock/gpt-5.6-sol": "us-east-1",
        }
    }
    models = ["openai/bedrock/gpt-5.5", "openai/bedrock/gpt-5.6-sol"]
    picks = {_region_for_run(cfg, models) for _ in range(5)}
    assert picks == {"us-east-1"}, "must sort, not depend on dict order"


def test_task_file_scan_fallback_does_not_raise_name_error():
    """The no-JSON-config branch referenced an undefined `task_content`.

    It raised NameError instead of scanning, so a config missing its sibling
    JSON silently got "no models" rather than the intended fallback.
    """
    import inspect as _inspect

    from eval_mcp.tools import run_eval

    src = _inspect.getsource(run_eval.handle_run_evaluation)
    idx = src.index("Fallback: scan the task .py")
    assert "task_content = Path(task_file).read_text()" in src[idx : idx + 600]
