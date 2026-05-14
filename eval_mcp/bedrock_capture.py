"""Capture Bedrock LLM calls and inject into Inspect AI transcript.

Uses the official OpenTelemetry botocore instrumentation to observe all boto3
Bedrock calls: Converse, ConverseStream, InvokeModel, InvokeModelWithResponseStream.
Captured data is converted to Inspect's ModelEvent format and written to the
active transcript for trajectory scoring.

Usage:
    from eval_mcp.bedrock_capture import bedrock_capture

    @solver
    def my_solver():
        async def solve(state, generate):
            with bedrock_capture():
                result = run_any_agent(state.input_text)
            state.output.completion = result
            return state
        return solve

Works with any framework calling Bedrock via boto3: Strands, LangChain, custom agents.
No code modification needed for the agent.
"""

import contextlib
import json
import os
from datetime import datetime, timezone
from typing import Any

from opentelemetry.sdk.trace import TracerProvider, ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor, LogExporter, LogExportResult

from inspect_ai.event._model import ModelEvent
from inspect_ai.log._transcript import transcript
from inspect_ai.model._chat_message import (
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
)
from inspect_ai.model._generate_config import GenerateConfig
from inspect_ai.model._model_output import (
    ChatCompletionChoice,
    ModelOutput,
    ModelUsage,
)
from inspect_ai.tool._tool_call import ToolCall
from inspect_ai.tool._tool_info import ToolInfo
from inspect_ai.tool._tool_params import ToolParams


def _extract_text(content: Any) -> str:
    """Pull plain text out of OTel content, which can be a string or a list
    of content blocks (each block a dict with a 'text' key, or a raw string).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                parts.append(str(block["text"]))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


def _build_tool_calls(raw: Any) -> list[ToolCall]:
    """Convert OTel tool_calls (list of dicts) to Inspect ToolCall objects.

    Shape per botocore's bedrock_utils.extract_tool_calls:
        [{"id": ..., "function": {"name": ..., "arguments": {...}}}, ...]
    """
    out: list[ToolCall] = []
    if not isinstance(raw, list):
        return out
    for tc in raw:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function", {}) or {}
        out.append(ToolCall(
            id=tc.get("id", ""),
            function=fn.get("name", ""),
            arguments=fn.get("arguments", {}) or {},
            type="function",
        ))
    return out


class _InspectLogExporter(LogExporter):
    """Convert OTel log records emitted by botocore's Bedrock instrumentation
    into Inspect ModelEvents.

    Each converse call emits one log record per message, driven by event_name:
      gen_ai.system.message      → ChatMessageSystem
      gen_ai.user.message        → ChatMessageUser
      gen_ai.assistant.message   → ChatMessageAssistant (with optional tool_calls)
      gen_ai.tool.message        → ChatMessageTool (tool result)
      gen_ai.choice              → flush: emit a ModelEvent whose `input` is the
                                    accumulated history and whose `output` is
                                    the choice's message.

    We drive off `event_name` (the authoritative role signal in the OTel GenAI
    semconv) instead of inferring from body shape, so every role survives and
    tool_call IDs stay linked.

    The previous implementation collapsed everything to a single `_pending_input`
    dict that got overwritten on every record, silently dropping system prompts,
    prior assistant turns, and tool results. This one accumulates a list and
    resets only when a `gen_ai.choice` record flushes it.
    """

    def __init__(self):
        self._pending_messages: list[Any] = []

    def export(self, batch):
        for record in batch:
            try:
                self._process_record(record)
            except Exception:
                pass
        return LogExportResult.SUCCESS

    def shutdown(self):
        pass

    def _process_record(self, record):
        event_name = getattr(record.log_record, "event_name", None) or ""
        body = record.log_record.body

        if event_name == "gen_ai.choice":
            self._flush_as_model_event(body)
            return

        # Everything else is an input-history message. Only the roles the
        # Bedrock instrumentation emits are handled; unknown event names are
        # ignored rather than mapped incorrectly.
        message = self._record_to_message(event_name, body)
        if message is not None:
            self._pending_messages.append(message)

    def _record_to_message(self, event_name: str, body: Any):
        """Map one input-side record to a ChatMessage*. Returns None for
        event names we don't handle, so future OTel additions don't blow up.
        """
        if not isinstance(body, dict):
            body = {}

        if event_name == "gen_ai.system.message":
            return ChatMessageSystem(content=_extract_text(body.get("content")))

        if event_name == "gen_ai.user.message":
            return ChatMessageUser(content=_extract_text(body.get("content")))

        if event_name == "gen_ai.assistant.message":
            return ChatMessageAssistant(
                content=_extract_text(body.get("content")),
                tool_calls=_build_tool_calls(body.get("tool_calls")) or None,
            )

        if event_name == "gen_ai.tool.message":
            return ChatMessageTool(
                tool_call_id=body.get("id"),
                content=_extract_text(body.get("content")),
            )

        return None

    def _flush_as_model_event(self, choice_body: Any):
        """Emit a ModelEvent using the accumulated history as input and the
        choice as output. Always resets state so the next call starts clean.
        """
        if not isinstance(choice_body, dict):
            choice_body = {}

        message = choice_body.get("message", {}) or {}
        finish_reason = choice_body.get("finish_reason", "end_turn")

        reason_map = {
            "end_turn": "stop",
            "tool_use": "tool_calls",
            "max_tokens": "max_tokens",
            "stop": "stop",
        }
        stop_reason = reason_map.get(finish_reason, "stop")

        output = ModelOutput(
            choices=[ChatCompletionChoice(
                message=ChatMessageAssistant(
                    content=_extract_text(message.get("content", "")),
                    tool_calls=_build_tool_calls(message.get("tool_calls")) or None,
                ),
                stop_reason=stop_reason,
            )],
            usage=ModelUsage(input_tokens=0, output_tokens=0, total_tokens=0),
        )

        input_messages = self._pending_messages or [ChatMessageUser(content="")]

        event = ModelEvent(
            model="bedrock",
            input=input_messages,
            tools=[],
            tool_choice="auto",
            config=GenerateConfig(),
            output=output,
            completed=datetime.now(timezone.utc),
        )

        # Reset before dispatch: if the transcript() call raises we still
        # want the next converse call to start with a clean slate.
        self._pending_messages = []

        try:
            transcript()._event(event)
        except Exception:
            pass


class _InspectSpanExporter(SpanExporter):
    """Captures span-level attributes (model, tokens, tools) and enriches ModelEvents."""

    def __init__(self, log_exporter: _InspectLogExporter):
        self._log_exporter = log_exporter

    def export(self, spans):
        for span in spans:
            try:
                self._process_span(span)
            except Exception:
                pass
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass

    def _process_span(self, span: ReadableSpan):
        attrs = dict(span.attributes or {})
        model = attrs.get("gen_ai.request.model")
        if not model:
            return

        input_tokens = int(attrs.get("gen_ai.usage.input_tokens", 0))
        output_tokens = int(attrs.get("gen_ai.usage.output_tokens", 0))

        # Update the most recently emitted ModelEvent with span-level data
        try:
            events = transcript().events
            for event in reversed(events):
                if isinstance(event, ModelEvent) and event.model == "bedrock":
                    provider = attrs.get("gen_ai.system", "bedrock")
                    event.model = f"{provider}/{model}"
                    if event.output and event.output.usage:
                        event.output.usage.input_tokens = input_tokens
                        event.output.usage.output_tokens = output_tokens
                        event.output.usage.total_tokens = input_tokens + output_tokens
                    break
        except Exception:
            pass


@contextlib.contextmanager
def bedrock_capture():
    """Context manager that captures all Bedrock calls into Inspect's transcript.

    Uses the official OpenTelemetry botocore instrumentation for complete coverage of:
    - converse / converse_stream
    - invoke_model / invoke_model_with_response_stream
    - All Bedrock model providers (Claude, Nova, Titan, Llama, Mistral, etc.)

    All captured LLM calls are written as ModelEvents to the active Inspect
    transcript, enabling trajectory scoring.

    Usage:
        with bedrock_capture():
            result = my_agent.run("question")
    """
    from opentelemetry.instrumentation.botocore import BotocoreInstrumentor

    # Enable content capture
    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "true"

    # Set up exporters
    log_exporter = _InspectLogExporter()
    span_exporter = _InspectSpanExporter(log_exporter)

    # Logger provider for message content (emitted as OTel log events)
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))

    # Tracer provider for span attributes (model, tokens, finish reason)
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))

    # Instrument botocore (includes Bedrock extension)
    instrumentor = BotocoreInstrumentor()
    instrumentor.instrument(
        tracer_provider=tracer_provider,
        logger_provider=logger_provider,
    )

    try:
        yield
    finally:
        instrumentor.uninstrument()
        tracer_provider.shutdown()
        logger_provider.shutdown()
