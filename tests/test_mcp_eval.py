"""Pytest wrapper for the mcp-builder Phase 4 evaluation.

The actual logic lives in `tests/mcp_eval/runner.py` — this just runs it
and asserts on the pass rate. Skipped automatically when AWS Bedrock isn't
reachable, so contributors without Bedrock access can still run the rest
of the suite.

Run only this:    uv run pytest tests/test_mcp_eval.py -v -s
Run from runner:  uv run python -m tests.mcp_eval.runner
"""

from __future__ import annotations

import asyncio
import os

import pytest

from tests.mcp_eval.runner import format_summary, run


def _bedrock_reachable() -> bool:
    """True iff boto3 can resolve AWS credentials. The runner will surface
    a Bedrock-specific failure (model not enabled, etc.) when it actually
    invokes the model — this check just keeps contributors-without-creds
    from seeing scary auth tracebacks."""
    try:
        import boto3

        session = boto3.Session(region_name=os.environ.get("AWS_REGION", "us-west-2"))
        creds = session.get_credentials()
        return creds is not None and creds.access_key is not None
    except Exception:
        return False


@pytest.mark.skipif(
    not _bedrock_reachable(),
    reason="Bedrock / AWS credentials not available — skipping live MCP eval.",
)
def test_mcp_eval_pass_rate():
    """Run the 10-question eval. Expect at least 7/10 to pass — leaves
    headroom for the LLM judge's occasional false-negative without
    masking a real regression in tool descriptions."""
    results = asyncio.run(run())
    summary = format_summary(results)
    print(summary)
    passed = sum(1 for r in results if r.passed)
    assert passed >= 7, (
        f"Only {passed}/{len(results)} questions passed.\n{summary}"
    )
