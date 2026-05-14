"""Tests for the cold-storage JSONL writer in otlp_receiver.

Cold storage is the insurance policy: if the projection layer
(`bedrock_capture._InspectLogExporter`) ever drops data due to a future
adapter bug, the raw OTel records are still on disk in append-only JSONL
form and can be re-projected offline without re-running the eval.

Tests cover:
  - The writer itself (`_JsonlWriter`): atomicity under concurrent writes,
    parent dir auto-creation, graceful failure when disk fails.
  - Receiver integration: spans/logs received via /v1/traces and /v1/logs
    end up in the JSONL file with the right shape.
  - Env-var pickup: setting EVAL_MCP_RAW_OTEL_PATH before start_receiver()
    triggers cold storage without an explicit kwarg.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx
import pytest

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

from eval_mcp.otlp_receiver import _JsonlWriter, start_receiver


# ---------------------------------------------------------------------------
# Helpers — recursive AnyValue construction so list-of-dict bodies (the
# botocore Bedrock instrumentation shape) round-trip correctly.
# ---------------------------------------------------------------------------


def _any(v) -> AnyValue:
    if isinstance(v, bool):
        return AnyValue(bool_value=v)
    if isinstance(v, int):
        return AnyValue(int_value=v)
    if isinstance(v, float):
        return AnyValue(double_value=v)
    if isinstance(v, str):
        return AnyValue(string_value=v)
    if isinstance(v, list):
        return AnyValue(array_value=ArrayValue(values=[_any(x) for x in v]))
    if isinstance(v, dict):
        return AnyValue(kvlist_value=KeyValueList(
            values=[KeyValue(key=k, value=_any(val)) for k, val in v.items()]
        ))
    raise TypeError(f"unsupported value type: {type(v)}")


def _kv(key: str, val) -> KeyValue:
    return KeyValue(key=key, value=_any(val))


def _kvlist(d: dict) -> AnyValue:
    return _any(d)


# ---------------------------------------------------------------------------
# _JsonlWriter — unit tests
# ---------------------------------------------------------------------------


def test_jsonl_writer_appends_each_record_on_its_own_line(tmp_path: Path):
    path = tmp_path / "raw.jsonl"
    w = _JsonlWriter(str(path))

    w.write("span", {"attributes": {"gen_ai.system": "aws.bedrock"}})
    w.write("log", {"event_name": "gen_ai.user.message", "body": {"content": "hi"}})

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    span_rec = json.loads(lines[0])
    log_rec = json.loads(lines[1])
    assert span_rec["kind"] == "span"
    assert span_rec["attributes"] == {"gen_ai.system": "aws.bedrock"}
    assert "received_at" in span_rec
    assert log_rec["kind"] == "log"
    assert log_rec["event_name"] == "gen_ai.user.message"


def test_jsonl_writer_creates_parent_dirs(tmp_path: Path):
    """Caller passes a path under a directory that doesn't exist yet —
    we shouldn't make them mkdir manually."""
    path = tmp_path / "deep" / "nested" / "raw.jsonl"
    assert not path.parent.exists()

    w = _JsonlWriter(str(path))
    w.write("span", {"attributes": {}})

    assert path.exists()


def test_jsonl_writer_serializes_concurrent_writes(tmp_path: Path):
    """Uvicorn dispatches OTLP requests across worker threads. If the writer
    isn't locked, two threads writing simultaneously can interleave bytes
    inside a single record. Pin the lock contract.
    """
    path = tmp_path / "concurrent.jsonl"
    w = _JsonlWriter(str(path))

    def worker(tid: int):
        for i in range(50):
            w.write("span", {"attributes": {"tid": tid, "i": i}})

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Each line must be parseable JSON — no interleaved bytes.
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 4 * 50
    for line in lines:
        rec = json.loads(line)  # raises if interleaved
        assert rec["kind"] == "span"


def test_jsonl_writer_swallows_unserializable_payload(tmp_path: Path):
    """If a payload contains a non-JSON-serializable thing (e.g. a bytes
    blob), we still log a record so we know an event happened — better
    than crashing the receiver mid-eval."""
    path = tmp_path / "weird.jsonl"
    w = _JsonlWriter(str(path))

    class Unserializable:
        pass

    w.write("span", {"attributes": {"weird": Unserializable()}})

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    # Either the payload survived via default=str, or we have the fallback
    # repr marker — either way, the line was written.
    assert rec["kind"] == "span"


def test_jsonl_writer_swallows_disk_errors(tmp_path: Path, monkeypatch):
    """If the disk fills up or perms break mid-eval, the receiver should
    keep accepting traffic — cold storage is best-effort, not load-bearing.
    """
    path = tmp_path / "raw.jsonl"
    w = _JsonlWriter(str(path))

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr("builtins.open", boom)

    # Should not raise.
    w.write("span", {"attributes": {"x": 1}})


# ---------------------------------------------------------------------------
# Integration — start_receiver + JSONL writer end-to-end
# ---------------------------------------------------------------------------


def _post_trace(url: str, attributes: dict) -> None:
    span = Span(attributes=[_kv(k, v) for k, v in attributes.items()])
    req = ExportTraceServiceRequest(
        resource_spans=[ResourceSpans(scope_spans=[ScopeSpans(spans=[span])])]
    )
    resp = httpx.post(
        f"{url}/v1/traces",
        content=req.SerializeToString(),
        headers={"content-type": "application/x-protobuf"},
        timeout=5,
    )
    resp.raise_for_status()


def _post_log(url: str, event_name: str, body: dict) -> None:
    record = LogRecord(body=_kvlist(body), event_name=event_name)
    req = ExportLogsServiceRequest(
        resource_logs=[ResourceLogs(scope_logs=[ScopeLogs(log_records=[record])])]
    )
    resp = httpx.post(
        f"{url}/v1/logs",
        content=req.SerializeToString(),
        headers={"content-type": "application/x-protobuf"},
        timeout=5,
    )
    resp.raise_for_status()


def test_receiver_writes_received_spans_and_logs_to_jsonl(tmp_path: Path):
    """The contract that matters: spans + logs received over OTLP/HTTP show
    up in the cold-storage JSONL with their decoded attributes/body intact.
    """
    raw_path = tmp_path / "run.jsonl"
    handle = start_receiver(raw_otel_path=str(raw_path))
    try:
        _post_trace(handle.url, {
            "gen_ai.system": "aws.bedrock",
            "gen_ai.request.model": "us.anthropic.claude-haiku-4-5",
            "gen_ai.usage.input_tokens": 42,
        })
        _post_log(handle.url, "gen_ai.user.message", {"content": [{"text": "hello"}]})
    finally:
        handle.shutdown()

    lines = raw_path.read_text().strip().splitlines()
    kinds = [json.loads(l)["kind"] for l in lines]
    assert "span" in kinds
    assert "log" in kinds

    span_rec = next(json.loads(l) for l in lines if json.loads(l)["kind"] == "span")
    assert span_rec["attributes"]["gen_ai.system"] == "aws.bedrock"
    assert span_rec["attributes"]["gen_ai.usage.input_tokens"] == 42

    log_rec = next(json.loads(l) for l in lines if json.loads(l)["kind"] == "log")
    assert log_rec["event_name"] == "gen_ai.user.message"
    assert log_rec["body"] == {"content": [{"text": "hello"}]}


def test_receiver_picks_up_path_from_env_var(tmp_path: Path, monkeypatch):
    """Templated solver code calls `start_receiver()` with no args; the
    cold-storage path comes from EVAL_MCP_RAW_OTEL_PATH set by
    handle_run_evaluation. This is the path that has to work in production.
    """
    raw_path = tmp_path / "via_env.jsonl"
    monkeypatch.setenv("EVAL_MCP_RAW_OTEL_PATH", str(raw_path))

    handle = start_receiver()  # no kwarg!
    try:
        _post_trace(handle.url, {"gen_ai.system": "aws.bedrock"})
    finally:
        handle.shutdown()

    assert raw_path.exists()
    lines = raw_path.read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["kind"] == "span"


def test_receiver_does_not_create_file_when_no_path_set(tmp_path: Path, monkeypatch):
    """No env, no kwarg — no cold-storage file. We don't want to silently
    write to a default path nobody asked for."""
    monkeypatch.delenv("EVAL_MCP_RAW_OTEL_PATH", raising=False)

    handle = start_receiver()
    try:
        _post_trace(handle.url, {"gen_ai.system": "aws.bedrock"})
    finally:
        handle.shutdown()

    # No file should have been created in tmp_path (the default cwd write
    # location for typos in the implementation).
    files = list(tmp_path.iterdir())
    assert files == [], f"unexpected cold-storage files: {files}"
