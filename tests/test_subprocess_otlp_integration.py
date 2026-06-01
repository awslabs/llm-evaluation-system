"""End-to-end: real subprocess → real boto3 Bedrock call → harness OTLP receiver.

The piece that proves the architecture. The other test files exercise the
receiver and runner in isolation with fakes; this one wires them up and
fires a real Bedrock Converse call, then asserts the receiver captured a
span with the expected gen_ai.request.model attribute set.

Skipped automatically when AWS credentials aren't reachable so CI machines
without Bedrock access don't fail. Locally: run with `AWS_PROFILE=...` set.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _aws_creds_available() -> bool:
    """Cheap check — try to load AWS config without actually making a network
    call. Returns False if there's no profile, env var, or instance profile
    that boto3 would resolve.
    """
    try:
        import boto3
        # Default the region like the other Bedrock-gated guards, so a box
        # with creds but no AWS_REGION env (e.g. an EC2 instance profile)
        # runs this instead of spuriously skipping on an unresolved region.
        session = boto3.Session(region_name=os.environ.get("AWS_REGION", "us-west-2"))
        creds = session.get_credentials()
        return creds is not None and session.region_name is not None
    except Exception:
        return False


_AGENT_BODY = '''\
"""Test agent: one real Bedrock Converse call. Used by the integration test
to verify OTLP spans flow from a subprocess back to the harness receiver.
"""
import os
import boto3


def run_agent(prompt: str) -> str:
    client = boto3.client(
        "bedrock-runtime",
        region_name=os.environ.get("AWS_REGION", "us-west-2"),
    )
    response = client.converse(
        modelId="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 50},
    )
    return response["output"]["message"]["content"][0]["text"]
'''


@pytest.mark.skipif(
    not _aws_creds_available(),
    reason="No AWS credentials / region — Bedrock integration test skipped.",
)
def test_real_boto3_call_in_subprocess_emits_span_to_receiver(tmp_path: Path):
    """The full flow: real Bedrock call inside an isolated subprocess →
    span lands at the in-harness OTLP receiver → capturing exporter sees a
    gen_ai.request.model attribute matching the model we invoked.
    """
    from opentelemetry.sdk._logs.export import LogExportResult
    from opentelemetry.sdk.trace.export import SpanExportResult

    from eval_mcp.otlp_receiver import start_receiver
    from eval_mcp.subprocess_runner import run_agent_subprocess

    class _Capture:
        def __init__(self):
            self.spans: list = []
            self.logs: list = []

        def export_spans(self, spans):
            self.spans.extend(spans)
            return SpanExportResult.SUCCESS

        def export_logs(self, records):
            self.logs.extend(records)
            return LogExportResult.SUCCESS

        def shutdown(self):
            pass

    cap = _Capture()

    class _SpanExp:
        export = staticmethod(cap.export_spans)
        shutdown = staticmethod(cap.shutdown)

    class _LogExp:
        export = staticmethod(cap.export_logs)
        shutdown = staticmethod(cap.shutdown)

    handle = start_receiver(_SpanExp, _LogExp, host="127.0.0.1", port=0)
    try:
        agent_file = tmp_path / "agent.py"
        agent_file.write_text(_AGENT_BODY)
        reqs_file = tmp_path / "requirements.txt"
        # boto3 is the only extra the fixture agent needs. The OTel deps are
        # auto-injected by build_command via --with, so they're not listed here.
        reqs_file.write_text("boto3\n")

        output = run_agent_subprocess(
            agent_path=str(agent_file),
            agent_entry="run_agent",
            prompt="Reply with just the word PONG.",
            otlp_endpoint=handle.url,
            sample_id="integration-1",
            requirements_path=str(reqs_file),
            timeout=120,
        )

        # The agent returned something — proves the subprocess+launcher path
        # works for real boto3 calls, not just stdlib fixtures.
        assert output, "agent returned empty output"

        # Allow a moment for the OTel SDK's BatchSpanProcessor to flush before
        # we inspect. The agent process has already exited (flush-on-shutdown
        # is automatic) but spans arrive asynchronously on this side.
        import time
        for _ in range(20):
            if cap.spans:
                break
            time.sleep(0.1)

        assert cap.spans, "no spans arrived at the receiver"

        # At least one span should be the Bedrock Converse call with the
        # model we asked for. There may also be HTTP-client spans (httpx,
        # urllib3) we don't care about — filter for the gen_ai signal.
        bedrock_spans = [
            s for s in cap.spans
            if (s.attributes or {}).get("gen_ai.request.model")
        ]
        assert bedrock_spans, (
            f"no gen_ai spans found; got attribute keys: "
            f"{[list((s.attributes or {}).keys()) for s in cap.spans]}"
        )
        model = bedrock_spans[0].attributes["gen_ai.request.model"]
        assert "claude-haiku-4-5" in model, (
            f"expected haiku-4-5 in span model, got: {model!r}"
        )
    finally:
        handle.shutdown()
