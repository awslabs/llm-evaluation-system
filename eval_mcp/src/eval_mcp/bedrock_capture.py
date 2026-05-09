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


class _InspectLogExporter(LogExporter):
    """Collects OTel log records (message content) emitted by Bedrock instrumentation."""

    def __init__(self):
        self._pending_input = None
        self._pending_span_model = None

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
        body = record.log_record.body
        if not body or not isinstance(body, dict):
            return

        # Input message (first record per call)
        if "content" in body and "message" not in body:
            self._pending_input = body
            return

        # Output message (second record per call — has finish_reason + message)
        if "message" in body or "finish_reason" in body:
            self._emit_model_event(body)

    def _emit_model_event(self, output_body: dict):
        """Build and emit a ModelEvent from captured input + output."""
        # Parse input
        input_messages = []
        if self._pending_input:
            content_blocks = self._pending_input.get("content", [])
            text = ""
            for block in content_blocks:
                if isinstance(block, dict) and "text" in block:
                    text += block["text"]
                elif isinstance(block, str):
                    text += block
            if text:
                input_messages.append(ChatMessageUser(content=text))
            self._pending_input = None

        # Parse output
        message = output_body.get("message", {})
        finish_reason = output_body.get("finish_reason", "end_turn")
        role = message.get("role", "assistant")
        content = message.get("content", "")
        tool_calls_raw = message.get("tool_calls", [])

        # Convert tool calls
        tool_calls = []
        for tc in tool_calls_raw:
            fn = tc.get("function", {})
            tool_calls.append(ToolCall(
                id=tc.get("id", ""),
                function=fn.get("name", ""),
                arguments=fn.get("arguments", {}),
                type="function",
            ))

        # Map finish reason
        reason_map = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "max_tokens", "stop": "stop"}
        stop_reason = reason_map.get(finish_reason, "stop")

        # Extract text from content
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "text" in block:
                    text += block["text"]

        output = ModelOutput(
            choices=[ChatCompletionChoice(
                message=ChatMessageAssistant(
                    content=text,
                    tool_calls=tool_calls if tool_calls else None,
                ),
                stop_reason=stop_reason,
            )],
            usage=ModelUsage(input_tokens=0, output_tokens=0, total_tokens=0),
        )

        event = ModelEvent(
            model="bedrock",
            input=input_messages or [ChatMessageUser(content="")],
            tools=[],
            tool_choice="auto",
            config=GenerateConfig(),
            output=output,
            completed=datetime.now(timezone.utc),
        )

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
