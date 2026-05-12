"""Tests for the in-harness OTLP receiver.

The receiver lets subprocess-isolated agents (their own venv, their own deps)
emit Bedrock-call telemetry back into the eval transcript without sharing
Python objects with the harness. It accepts OTLP/HTTP-protobuf on POST
/v1/traces and /v1/logs, decodes the protobuf, and dispatches each batch
through the existing _InspectSpanExporter / _InspectLogExporter so the
downstream scoring pipeline sees the exact same ModelEvents it does today.

Tests are split into:

  - Smoke: protobuf classes import (Phase 1 — guards the dep).
  - Adapter: protobuf → in-process ReadableSpan/LogRecord shape that the
    existing exporters consume (Phase 2a).
  - Receiver: FastAPI app accepts batches, dispatches to exporters (2b).
"""

from __future__ import annotations

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import (
    AnyValue,
    ArrayValue,
    KeyValue,
    KeyValueList,
)
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord, ResourceLogs, ScopeLogs
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span


# ---------------------------------------------------------------------------
# Phase 1 — smoke
# ---------------------------------------------------------------------------

def test_otlp_protobuf_classes_importable():
    """Phase 1: the deps we'll build on are present and stable.

    Guards against `opentelemetry-proto` going missing from pyproject or
    being incompatible with the SDK version we ship.
    """
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
        ExportTraceServiceResponse,
    )
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
        ExportLogsServiceRequest,
        ExportLogsServiceResponse,
    )
    from opentelemetry.proto.trace.v1.trace_pb2 import (
        ResourceSpans,
        ScopeSpans,
        Span,
    )
    from opentelemetry.proto.logs.v1.logs_pb2 import (
        ResourceLogs,
        ScopeLogs,
        LogRecord,
    )

    # Names match the public OTLP spec; if Astral or upstream ever rename
    # any of these, we want this test to scream so the receiver isn't
    # silently building stale messages.
    assert ExportTraceServiceRequest.DESCRIPTOR.full_name == (
        "opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest"
    )
    assert ExportLogsServiceRequest.DESCRIPTOR.full_name == (
        "opentelemetry.proto.collector.logs.v1.ExportLogsServiceRequest"
    )
    assert Span.DESCRIPTOR.full_name == "opentelemetry.proto.trace.v1.Span"
    assert LogRecord.DESCRIPTOR.full_name == "opentelemetry.proto.logs.v1.LogRecord"


# ---------------------------------------------------------------------------
# Phase 2a — adapter
#
# _InspectSpanExporter._process_span (bedrock_capture.py:176) reads exactly
# one thing off a span: `dict(span.attributes or {})`. _InspectLogExporter
# reads `record.log_record.body` and treats it as a dict. So the adapter's
# job is narrow: unpack protobuf AnyValue/KeyValueList into python
# primitives and present a duck-typed object with the right attribute
# accessors. These tests pin down each AnyValue branch the Bedrock
# instrumentation emits.
# ---------------------------------------------------------------------------


def _str(s: str) -> AnyValue:
    return AnyValue(string_value=s)


def _int(n: int) -> AnyValue:
    return AnyValue(int_value=n)


def _kvlist(d: dict) -> AnyValue:
    return AnyValue(
        kvlist_value=KeyValueList(
            values=[KeyValue(key=k, value=_any(v)) for k, v in d.items()]
        )
    )


def _array(items: list) -> AnyValue:
    return AnyValue(array_value=ArrayValue(values=[_any(v) for v in items]))


def _any(v) -> AnyValue:
    """Convert a python value to an OTel AnyValue. Mirrors what the SDK does."""
    if isinstance(v, bool):
        return AnyValue(bool_value=v)
    if isinstance(v, int):
        return _int(v)
    if isinstance(v, float):
        return AnyValue(double_value=v)
    if isinstance(v, str):
        return _str(v)
    if isinstance(v, list):
        return _array(v)
    if isinstance(v, dict):
        return _kvlist(v)
    raise TypeError(f"unsupported value type: {type(v)}")


def _build_trace_request(span_attributes: dict) -> ExportTraceServiceRequest:
    """Build a minimal valid ExportTraceServiceRequest with one span."""
    span = Span(
        trace_id=b"\x01" * 16,
        span_id=b"\x02" * 8,
        name="bedrock.converse",
        kind=Span.SPAN_KIND_CLIENT,
        attributes=[
            KeyValue(key=k, value=_any(v)) for k, v in span_attributes.items()
        ],
    )
    return ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(scope_spans=[ScopeSpans(spans=[span])]),
        ]
    )


def _build_logs_request(log_body: dict) -> ExportLogsServiceRequest:
    """Build a minimal valid ExportLogsServiceRequest with one record."""
    record = LogRecord(body=_kvlist(log_body))
    return ExportLogsServiceRequest(
        resource_logs=[
            ResourceLogs(scope_logs=[ScopeLogs(log_records=[record])]),
        ]
    )


def test_decode_trace_request_extracts_gen_ai_attributes():
    """Adapter unpacks the gen_ai.* attributes _InspectSpanExporter needs.

    The Bedrock botocore instrumentation emits spans with
    `gen_ai.request.model`, `gen_ai.usage.input_tokens`,
    `gen_ai.usage.output_tokens`, `gen_ai.system`. The adapter must round-
    trip those from protobuf to a python dict accessible as `.attributes`.
    """
    from eval_mcp.otlp_receiver import decode_trace_request

    req = _build_trace_request({
        "gen_ai.system": "aws.bedrock",
        "gen_ai.request.model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "gen_ai.usage.input_tokens": 42,
        "gen_ai.usage.output_tokens": 137,
    })

    spans = decode_trace_request(req)

    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs["gen_ai.system"] == "aws.bedrock"
    assert attrs["gen_ai.request.model"] == (
        "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    )
    assert attrs["gen_ai.usage.input_tokens"] == 42
    assert attrs["gen_ai.usage.output_tokens"] == 137


def test_decode_logs_request_extracts_nested_body():
    """Adapter unpacks nested KeyValueList bodies into python dicts.

    The Bedrock instrumentation emits each input/output message as one log
    record with a KeyValueList body that contains the same shape OpenAI's
    chat-completion records use: top-level `content`/`message`/`finish_reason`
    fields, where `content` is a list of dicts each with a `text` key.
    """
    from eval_mcp.otlp_receiver import decode_logs_request

    req = _build_logs_request({
        "content": [{"text": "What is 2+2?"}],
    })

    records = decode_logs_request(req)
    assert len(records) == 1
    body = records[0].log_record.body
    assert isinstance(body, dict)
    assert body["content"] == [{"text": "What is 2+2?"}]


def test_decode_logs_request_handles_output_with_finish_reason():
    """The output-record shape produced by Bedrock instrumentation: a
    `message` dict with `role`+`content`+optional `tool_calls`, plus a
    top-level `finish_reason`. The adapter must surface the same dict
    structure _InspectLogExporter._process_record expects.
    """
    from eval_mcp.otlp_receiver import decode_logs_request

    req = _build_logs_request({
        "message": {
            "role": "assistant",
            "content": [{"text": "4"}],
        },
        "finish_reason": "end_turn",
    })

    records = decode_logs_request(req)
    body = records[0].log_record.body
    assert body["message"]["role"] == "assistant"
    assert body["message"]["content"] == [{"text": "4"}]
    assert body["finish_reason"] == "end_turn"


def test_decoded_spans_are_consumed_by_inspect_span_exporter():
    """End-to-end shape check: spans the adapter produces are accepted by
    the real _InspectSpanExporter without modification — that's the entire
    point of the duck-typed wrapper.
    """
    from eval_mcp.bedrock_capture import _InspectLogExporter, _InspectSpanExporter
    from eval_mcp.otlp_receiver import decode_trace_request

    req = _build_trace_request({
        "gen_ai.system": "aws.bedrock",
        "gen_ai.request.model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "gen_ai.usage.input_tokens": 5,
        "gen_ai.usage.output_tokens": 3,
    })
    spans = decode_trace_request(req)

    # The exporter swallows exceptions, so we test that .export() returns
    # SUCCESS and the in-process attribute read in `_process_span` doesn't
    # raise. There's no transcript to populate (none active), but that path
    # is wrapped in try/except — the adapter contract is met as long as the
    # exporter accepts our objects.
    exporter = _InspectSpanExporter(_InspectLogExporter())
    result = exporter.export(spans)
    from opentelemetry.sdk.trace.export import SpanExportResult
    assert result == SpanExportResult.SUCCESS


# ---------------------------------------------------------------------------
# Phase 2b — FastAPI receiver
#
# The receiver hosts two POST endpoints (OTLP/HTTP-protobuf spec: /v1/traces
# and /v1/logs). Bodies are application/x-protobuf. Successful responses are
# 200 + protobuf-encoded Empty Export*ServiceResponse.
# ---------------------------------------------------------------------------


from opentelemetry.sdk._logs.export import LogExportResult
from opentelemetry.sdk.trace.export import SpanExportResult


class _CapturingSpanExporter:
    """Test double — records every batch the receiver dispatches."""

    def __init__(self):
        self.batches: list[list] = []

    def export(self, spans):
        self.batches.append(list(spans))
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


class _CapturingLogExporter:
    def __init__(self):
        self.batches: list[list] = []

    def export(self, records):
        self.batches.append(list(records))
        return LogExportResult.SUCCESS

    def shutdown(self):
        pass


def test_receiver_traces_endpoint_dispatches_to_span_exporter():
    """POST /v1/traces with a valid OTLP protobuf body calls the exporter
    once with the decoded spans and returns 200 + an empty protobuf body.
    """
    from fastapi.testclient import TestClient

    from eval_mcp.otlp_receiver import build_receiver_app

    span_exp = _CapturingSpanExporter()
    log_exp = _CapturingLogExporter()
    app = build_receiver_app(span_exp, log_exp)
    client = TestClient(app)

    req = _build_trace_request({
        "gen_ai.system": "aws.bedrock",
        "gen_ai.request.model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "gen_ai.usage.input_tokens": 7,
    })
    response = client.post(
        "/v1/traces",
        content=req.SerializeToString(),
        headers={"content-type": "application/x-protobuf"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-protobuf"
    # Empty success response per OTLP spec — no partial_success means everything accepted.
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceResponse,
    )
    parsed = ExportTraceServiceResponse()
    parsed.ParseFromString(response.content)
    assert not parsed.HasField("partial_success")

    assert len(span_exp.batches) == 1
    batch = span_exp.batches[0]
    assert len(batch) == 1
    assert batch[0].attributes["gen_ai.request.model"] == (
        "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    )


def test_receiver_logs_endpoint_dispatches_to_log_exporter():
    """POST /v1/logs with a valid OTLP protobuf body calls the log exporter."""
    from fastapi.testclient import TestClient

    from eval_mcp.otlp_receiver import build_receiver_app

    span_exp = _CapturingSpanExporter()
    log_exp = _CapturingLogExporter()
    app = build_receiver_app(span_exp, log_exp)
    client = TestClient(app)

    req = _build_logs_request({"content": [{"text": "ping"}]})
    response = client.post(
        "/v1/logs",
        content=req.SerializeToString(),
        headers={"content-type": "application/x-protobuf"},
    )

    assert response.status_code == 200
    assert len(log_exp.batches) == 1
    assert log_exp.batches[0][0].log_record.body == {"content": [{"text": "ping"}]}


def test_receiver_rejects_wrong_content_type():
    """OTLP/HTTP-protobuf spec: bodies must be application/x-protobuf.
    JSON-encoded OTLP exists but uses a different content-type; we serve only
    protobuf, so a JSON body should fail fast with 415.
    """
    from fastapi.testclient import TestClient

    from eval_mcp.otlp_receiver import build_receiver_app

    app = build_receiver_app(_CapturingSpanExporter(), _CapturingLogExporter())
    client = TestClient(app)

    response = client.post(
        "/v1/traces",
        content=b'{"resourceSpans":[]}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 415


def test_receiver_rejects_malformed_protobuf():
    """A protobuf decode failure should return 400, not 500. This is how
    OTel clients distinguish 'we sent garbage' from 'server is down'.
    """
    from fastapi.testclient import TestClient

    from eval_mcp.otlp_receiver import build_receiver_app

    span_exp = _CapturingSpanExporter()
    app = build_receiver_app(span_exp, _CapturingLogExporter())
    client = TestClient(app)

    response = client.post(
        "/v1/traces",
        content=b"\xff\xff\xff this is not a valid protobuf message \xff",
        headers={"content-type": "application/x-protobuf"},
    )
    assert response.status_code == 400
    assert span_exp.batches == []


def test_start_receiver_binds_port_accepts_post_and_shuts_down():
    """Lifecycle test: start_receiver() binds an ephemeral port, accepts a
    real HTTP POST end-to-end, and shutdown() stops the server cleanly so
    the port is free for the next test.
    """
    import httpx

    from eval_mcp.otlp_receiver import start_receiver

    span_exp = _CapturingSpanExporter()
    log_exp = _CapturingLogExporter()
    handle = start_receiver(span_exp, log_exp, host="127.0.0.1", port=0)
    try:
        # `handle.url` is the agent-facing OTEL_EXPORTER_OTLP_ENDPOINT value
        # — the OTel HTTP exporter appends /v1/traces and /v1/logs itself.
        assert handle.url.startswith("http://127.0.0.1:")

        req = _build_trace_request({
            "gen_ai.request.model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        })
        response = httpx.post(
            f"{handle.url}/v1/traces",
            content=req.SerializeToString(),
            headers={"content-type": "application/x-protobuf"},
            timeout=5.0,
        )
        assert response.status_code == 200
        assert len(span_exp.batches) == 1
    finally:
        handle.shutdown()

    # After shutdown, the port should be free — a follow-up request fails.
    with __import__("pytest").raises(httpx.ConnectError):
        httpx.post(f"{handle.url}/v1/traces", content=b"", timeout=1.0)
