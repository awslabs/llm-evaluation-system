"""Tests for the pre-flight canary check.

The canary's job is to detect silent capture failures before an eval runs:
spawn the agent once, look for at least one Bedrock LLM span in the receiver,
abort with a clear message if nothing landed.

Tests are at two layers:
  - `_has_llm_span` predicate: does it correctly identify provider spans?
  - `run_canary` end-to-end: does it spin up + tear down the receiver, run
    the agent, and report ok=True/False with the right diagnostic?

The end-to-end tests use a stdlib-only fixture agent (no AWS calls). The
"capture works" path is harder to test without real Bedrock — we'd have
to mock the OTel exporter — so we focus on the failure modes (agent crash,
agent runs but emits no spans), which are the ones the canary exists to
catch.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from eval_mcp.canary import _has_llm_span, run_canary


# ---------------------------------------------------------------------------
# _has_llm_span — pure predicate over decoded spans
# ---------------------------------------------------------------------------


@dataclass
class _FakeSpan:
    attributes: dict


def test_has_llm_span_recognizes_aws_bedrock():
    spans = [_FakeSpan({"gen_ai.system": "aws.bedrock", "gen_ai.request.model": "haiku"})]
    saw, count = _has_llm_span(spans)
    assert saw is True
    assert count == 1


def test_has_llm_span_ignores_framework_only_spans():
    """A strands-agents span without an aws.bedrock companion means the
    botocore instrumentor isn't capturing wire-level calls. We must NOT
    count framework spans alone as proof of working capture."""
    spans = [_FakeSpan({"gen_ai.system": "strands-agents"})]
    saw, count = _has_llm_span(spans)
    assert saw is False
    assert count == 0


def test_has_llm_span_counts_multiple_provider_spans():
    """A tool-using turn produces multiple converse calls, each with its
    own aws.bedrock span. Count them all."""
    spans = [
        _FakeSpan({"gen_ai.system": "aws.bedrock"}),
        _FakeSpan({"gen_ai.system": "aws.bedrock"}),
        _FakeSpan({"gen_ai.system": "strands-agents"}),  # framework, doesn't count
    ]
    saw, count = _has_llm_span(spans)
    assert saw is True
    assert count == 2


def test_has_llm_span_handles_empty_list():
    saw, count = _has_llm_span([])
    assert saw is False
    assert count == 0


def test_has_llm_span_ignores_non_genai_spans():
    """HTTP / db / generic spans must not register as LLM spans."""
    spans = [
        _FakeSpan({"http.method": "POST"}),
        _FakeSpan({"db.system": "postgresql"}),
        _FakeSpan({}),
    ]
    saw, count = _has_llm_span(spans)
    assert saw is False
    assert count == 0


# ---------------------------------------------------------------------------
# run_canary — integration with the real subprocess runner + receiver
# ---------------------------------------------------------------------------


def test_run_canary_reports_failure_when_agent_crashes(tmp_path: Path):
    """If the agent raises on the canary prompt, the eval would fail on
    every real sample too. Surface the agent's stderr so the user can
    fix it before paying for N samples."""
    agent_file = tmp_path / "agent.py"
    agent_file.write_text(
        'def run_agent(prompt):\n'
        '    raise RuntimeError("agent is broken")\n'
    )

    result = run_canary(
        agent_path=str(agent_file),
        agent_entry="run_agent",
        timeout=30.0,
    )

    assert result.ok is False
    # Agent crashed — must surface the actual error, not generic "no spans"
    assert result.error is not None
    assert "crashed" in result.error.lower()
    assert result.agent_stderr is not None
    assert "agent is broken" in result.agent_stderr


def test_run_canary_reports_failure_when_no_llm_spans_captured(tmp_path: Path):
    """The exact bug the canary exists to catch: agent runs successfully
    but doesn't make any Bedrock calls (or capture is broken). The eval
    would silently produce empty scores — we must abort."""
    agent_file = tmp_path / "agent.py"
    agent_file.write_text(
        'def run_agent(prompt):\n'
        '    return f"echo: {prompt}"\n'  # no Bedrock call at all
    )

    result = run_canary(
        agent_path=str(agent_file),
        agent_entry="run_agent",
        timeout=30.0,
    )

    assert result.ok is False
    assert result.llm_spans_seen == 0
    assert result.error is not None
    # Diagnostic must mention the actionable causes so the user knows
    # where to look.
    assert "no Bedrock LLM spans" in result.error
    # Must not surface fake stderr — the agent didn't crash, it just
    # didn't call Bedrock.
    assert result.agent_stderr is None


def test_run_canary_returns_clean_result_object(tmp_path: Path):
    """Smoke test: even on the failure path, the result has all the
    fields a caller might want to surface (spans_seen, llm_spans_seen,
    error). Guards against AttributeError when the caller renders the
    error message."""
    agent_file = tmp_path / "agent.py"
    agent_file.write_text('def run_agent(p): return "x"\n')

    result = run_canary(
        agent_path=str(agent_file),
        agent_entry="run_agent",
        timeout=30.0,
    )

    # All fields should be set, not None where the caller expects an int.
    assert isinstance(result.ok, bool)
    assert isinstance(result.spans_seen, int)
    assert isinstance(result.llm_spans_seen, int)
