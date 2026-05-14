"""In-harness OTLP receiver for subprocess-isolated agent evals.

Subprocess-mode agents run in their own venv (whatever framework/version they
need) and emit Bedrock-call telemetry over OTLP/HTTP-protobuf back to the
harness. This module decodes those protobuf payloads into the duck-typed
objects that the existing _InspectSpanExporter / _InspectLogExporter in
bedrock_capture.py already know how to consume — meaning the downstream
scoring pipeline stays unchanged.

The adapters here are deliberately narrow. The exporters touch only:
  - span.attributes (read as a dict)
  - record.log_record.body (read as a dict)
So we present exactly those shapes and nothing more.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from google.protobuf.message import DecodeError

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue


# Env var the solver-side receiver reads to discover the cold-storage path.
# Set by handle_run_evaluation before spawning the Inspect subprocess; lets
# us thread the path down without changing every templated solver.
_RAW_OTEL_ENV = "EVAL_MCP_RAW_OTEL_PATH"


# ---------------------------------------------------------------------------
# Duck-typed wrappers for the existing exporters
# ---------------------------------------------------------------------------


@dataclass
class _DecodedSpan:
    """Shape consumed by _InspectSpanExporter._process_span — only `.attributes`."""

    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class _DecodedLogRecord:
    """Shape consumed by _InspectLogExporter._process_record.

    `body` is the unpacked log body. `event_name` is OTel's authoritative
    role signal for GenAI records (gen_ai.user.message, gen_ai.choice, etc.)
    — the adapter relies on it to map records to ChatMessage* types
    correctly. Forgetting to thread this field through means every record
    gets dropped silently.
    """

    body: dict[str, Any] | None = None
    event_name: str | None = None


@dataclass
class _DecodedLogData:
    log_record: _DecodedLogRecord


# ---------------------------------------------------------------------------
# AnyValue / KeyValueList unpacking
#
# OTLP attributes and log bodies are typed via AnyValue (oneof) and recursive
# KeyValueList. The Bedrock instrumentation uses kvlist bodies whose leaves
# are strings / ints / arrays-of-kvlists, so we need a fully recursive
# unpack — not just the top level.
# ---------------------------------------------------------------------------


def _decode_any(v: AnyValue) -> Any:
    """Convert one OTLP AnyValue into the matching python primitive.

    Returns None for an unset AnyValue (the protobuf default), which is what
    the existing log exporter checks for via `if not body`.
    """
    which = v.WhichOneof("value")
    if which is None:
        return None
    if which == "string_value":
        return v.string_value
    if which == "bool_value":
        return v.bool_value
    if which == "int_value":
        return v.int_value
    if which == "double_value":
        return v.double_value
    if which == "bytes_value":
        return v.bytes_value
    if which == "array_value":
        return [_decode_any(item) for item in v.array_value.values]
    if which == "kvlist_value":
        return _decode_kv_list(v.kvlist_value.values)
    return None


def _decode_kv_list(kvs) -> dict[str, Any]:
    """Convert a KeyValueList (list of KeyValue) to a python dict."""
    return {kv.key: _decode_any(kv.value) for kv in kvs}


def _decode_attributes(attrs: list[KeyValue]) -> dict[str, Any]:
    """Flatten a span/log/resource attribute list into a python dict."""
    return _decode_kv_list(attrs)


# ---------------------------------------------------------------------------
# Public decoders — consumed by the HTTP receiver (Phase 2b)
# ---------------------------------------------------------------------------


def decode_trace_request(req: ExportTraceServiceRequest) -> list[_DecodedSpan]:
    """Flatten an OTLP trace export into the duck-typed spans the existing
    _InspectSpanExporter understands.

    OTLP wraps spans in two layers — ResourceSpans → ScopeSpans → Span — so
    callers don't need to walk that tree themselves.
    """
    spans: list[_DecodedSpan] = []
    for rs in req.resource_spans:
        for ss in rs.scope_spans:
            for span in ss.spans:
                spans.append(_DecodedSpan(attributes=_decode_attributes(span.attributes)))
    return spans


def decode_logs_request(req: ExportLogsServiceRequest) -> list[_DecodedLogData]:
    """Flatten an OTLP logs export into the duck-typed records the existing
    _InspectLogExporter understands.

    The Bedrock instrumentation puts each chat message in one record whose
    body is a KeyValueList. We unpack that recursively so the exporter sees
    a plain python dict.
    """
    records: list[_DecodedLogData] = []
    for rl in req.resource_logs:
        for sl in rl.scope_logs:
            for record in sl.log_records:
                body = _decode_any(record.body)
                # event_name is the authoritative role signal for GenAI
                # log records (gen_ai.user.message, gen_ai.choice, etc.).
                # Without it the downstream adapter can't tell roles apart.
                event_name = getattr(record, "event_name", None) or None
                records.append(_DecodedLogData(
                    log_record=_DecodedLogRecord(body=body, event_name=event_name),
                ))
    return records


# ---------------------------------------------------------------------------
# FastAPI receiver
#
# OTLP/HTTP-protobuf spec (https://opentelemetry.io/docs/specs/otlp/#otlphttp):
#   - POST <endpoint>/v1/traces  body: ExportTraceServiceRequest protobuf
#   - POST <endpoint>/v1/logs    body: ExportLogsServiceRequest protobuf
#   - Content-Type: application/x-protobuf on both request and response
#   - 200 with an empty Export*ServiceResponse means "all accepted"
# ---------------------------------------------------------------------------


_PROTOBUF_CT = "application/x-protobuf"


class _JsonlWriter:
    """Append-only writer for the cold-storage JSONL.

    Serializes writes across uvicorn worker threads with a single lock;
    sequential `open(path, "a")` is atomic at the syscall level, but we
    want to avoid interleaved bytes within a single record's write.

    Failures here are logged-but-swallowed so a disk problem can't take
    the receiver down — the in-memory buffer + exporter dispatch are the
    primary path; cold storage is insurance.
    """

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        # Make sure the parent dir exists eagerly; failing now beats failing
        # on every span write.
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, payload: dict) -> None:
        record = {"kind": kind, "received_at": time.time(), **payload}
        try:
            line = json.dumps(record, default=str)
        except (TypeError, ValueError):
            # If something in the payload isn't JSON-serializable, fall back
            # to a repr so we still capture the event existed. Better than
            # losing the record entirely.
            line = json.dumps({"kind": kind, "received_at": time.time(),
                               "_unserializable_repr": repr(payload)})
        try:
            with self._lock:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError:
            # Disk full, permission denied, etc. Cold storage is a "best
            # effort" guarantee; do not crash the receiver.
            pass


def _decoded_span_to_payload(span) -> dict:
    """Project a _DecodedSpan into a JSON-safe dict for cold storage."""
    return {"attributes": dict(getattr(span, "attributes", None) or {})}


def _decoded_log_to_payload(record) -> dict:
    """Project a _DecodedLogData into a JSON-safe dict for cold storage."""
    lr = record.log_record
    return {
        "event_name": getattr(lr, "event_name", None),
        "body": getattr(lr, "body", None),
    }


def build_receiver_app(
    span_exporter=None,
    log_exporter=None,
    buffer=None,
    raw_writer: Optional[_JsonlWriter] = None,
) -> FastAPI:
    """Build a FastAPI app that consumes OTLP/HTTP-protobuf.

    Each received batch is *always* appended to `buffer` (a (spans, logs)
    tuple of lists), giving callers a thread-safe place to read from in
    their own coroutine context. If `span_exporter` / `log_exporter` are
    provided as well, batches are also dispatched to them synchronously
    on the request thread (the legacy direct-dispatch mode, useful for
    test capture).

    If `raw_writer` is provided, every span and log is also appended to
    its JSONL file as-received. That cold-storage copy is the fallback
    path: if the projection layer (bedrock_capture._InspectLogExporter)
    drops data due to a future bug, the raw record is still on disk and
    re-derivable without re-running the eval.

    Why buffer-by-default: the receiver runs in a worker thread, so
    contextvar-based APIs like Inspect AI's `transcript()` are unreachable
    from inside the request handler. The buffer lets the caller drain in
    the right context.
    """
    app = FastAPI(title="eval-mcp OTLP receiver")
    span_buf, log_buf = ([], []) if buffer is None else buffer

    def _require_protobuf_ct(request: Request) -> None:
        ct = request.headers.get("content-type", "").split(";")[0].strip()
        if ct != _PROTOBUF_CT:
            raise HTTPException(
                status_code=415,
                detail=f"Content-Type must be {_PROTOBUF_CT}, got {ct or '(missing)'}",
            )

    @app.post("/v1/traces")
    async def _traces(request: Request) -> Response:
        _require_protobuf_ct(request)
        raw = await request.body()
        req = ExportTraceServiceRequest()
        try:
            req.ParseFromString(raw)
        except DecodeError as e:
            raise HTTPException(status_code=400, detail=f"protobuf decode: {e}")

        spans = decode_trace_request(req)
        if spans:
            span_buf.extend(spans)
            if span_exporter is not None:
                span_exporter.export(spans)
            if raw_writer is not None:
                for s in spans:
                    raw_writer.write("span", _decoded_span_to_payload(s))
        return Response(
            content=ExportTraceServiceResponse().SerializeToString(),
            media_type=_PROTOBUF_CT,
        )

    @app.post("/v1/logs")
    async def _logs(request: Request) -> Response:
        _require_protobuf_ct(request)
        raw = await request.body()
        req = ExportLogsServiceRequest()
        try:
            req.ParseFromString(raw)
        except DecodeError as e:
            raise HTTPException(status_code=400, detail=f"protobuf decode: {e}")

        records = decode_logs_request(req)
        if records:
            log_buf.extend(records)
            if log_exporter is not None:
                log_exporter.export(records)
            if raw_writer is not None:
                for r in records:
                    raw_writer.write("log", _decoded_log_to_payload(r))
        return Response(
            content=ExportLogsServiceResponse().SerializeToString(),
            media_type=_PROTOBUF_CT,
        )

    return app


# ---------------------------------------------------------------------------
# Lifecycle — bind a real port, run uvicorn in a background thread
# ---------------------------------------------------------------------------


@dataclass
class ReceiverHandle:
    """Started receiver. Pass `url` as OTEL_EXPORTER_OTLP_ENDPOINT to the
    agent subprocess; call `drain()` to pull buffered batches, and
    `shutdown()` when the eval is done.
    """

    url: str
    _server: uvicorn.Server
    _thread: threading.Thread
    _span_buf: list = field(default_factory=list)
    _log_buf: list = field(default_factory=list)

    def drain(self) -> tuple[list, list]:
        """Return + clear all spans and logs received so far.

        Call this after the agent subprocess has exited (and you've waited
        long enough for the final OTel flush to land). Returned data can
        be safely fed through `_InspectSpanExporter` / `_InspectLogExporter`
        in the caller's coroutine — that's how transcript() ends up
        pointing at the right sample.
        """
        spans, self._span_buf[:] = list(self._span_buf), []
        logs, self._log_buf[:] = list(self._log_buf), []
        return spans, logs

    def shutdown(self, timeout: float = 5.0) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=timeout)


def _find_free_port(host: str) -> int:
    """Ask the OS for an ephemeral port. Tiny race window vs binding it
    moments later, but acceptable for our use (no other listeners are
    racing for the same loopback port in microseconds).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def start_receiver(
    span_exporter=None,
    log_exporter=None,
    host: str = "127.0.0.1",
    port: int = 0,
    raw_otel_path: Optional[str] = None,
) -> ReceiverHandle:
    """Start the OTLP receiver in a background thread and return a handle.

    Pass `port=0` to bind an ephemeral port (the common case — every eval
    run picks its own port so concurrent runs don't collide). Received
    batches are always appended to internal buffers (drainable via
    `handle.drain()`); if exporters are also passed they get a synchronous
    side-call on the request thread for legacy direct-dispatch callers.

    If `raw_otel_path` is set (or the EVAL_MCP_RAW_OTEL_PATH env var is set),
    every received span and log is also appended to that JSONL file as
    cold storage. Failure to write doesn't break the receiver; it's
    a "best effort" archive that lets us re-derive ModelEvents offline if
    the projection layer is ever buggy. The env-var fallback exists so
    templated solver code (which we don't recompile per-run) can pick
    the path up automatically when handle_run_evaluation sets it.
    """
    if port == 0:
        port = _find_free_port(host)

    if raw_otel_path is None:
        raw_otel_path = os.environ.get(_RAW_OTEL_ENV) or None
    raw_writer = _JsonlWriter(raw_otel_path) if raw_otel_path else None

    span_buf: list = []
    log_buf: list = []
    app = build_receiver_app(
        span_exporter, log_exporter,
        buffer=(span_buf, log_buf),
        raw_writer=raw_writer,
    )
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="otlp-receiver", daemon=True)
    thread.start()

    # Wait for uvicorn to bind. server.started becomes True once startup is
    # complete; we spin briefly so the caller can hand the URL to a subprocess
    # without a race.
    deadline = 5.0
    waited = 0.0
    while not server.started and waited < deadline:
        threading.Event().wait(0.02)
        waited += 0.02
    if not server.started:
        server.should_exit = True
        thread.join(timeout=1.0)
        raise RuntimeError("OTLP receiver failed to start within 5s")

    return ReceiverHandle(
        url=f"http://{host}:{port}",
        _server=server,
        _thread=thread,
        _span_buf=span_buf,
        _log_buf=log_buf,
    )
