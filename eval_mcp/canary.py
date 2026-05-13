"""Pre-flight capture check for agent evals.

Runs the user's agent ONCE with a trivial prompt before Inspect kicks off
the real eval. If we don't see at least one Bedrock span land in the OTLP
receiver, the eval is going to silently produce empty scores — abort now
with a clear message instead of wasting minutes running N samples that
all have nothing to capture.

Catches: missing/wrong instrumentor, OTel env-var leak that breaks the
agent's subprocess, agents that raise immediately, agents that don't
actually call Bedrock, network blocked between agent and receiver, etc.

Only runs for agent evals (where we control the subprocess + receiver).
Standard model evals route through Inspect's own model provider and
don't go through our OTel pipeline — they have their own failure modes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from eval_mcp.otlp_receiver import start_receiver
from eval_mcp.subprocess_runner import (
    AgentSubprocessError,
    run_agent_subprocess,
)


# OTel `gen_ai.system` values that count as "we captured a real LLM call."
# Adding a new provider (anthropic SDK, openai SDK) means appending here.
_LLM_SYSTEM_VALUES = (
    "aws.bedrock",
    # Framework-self-instrumented spans don't count on their own — they
    # often appear without the underlying provider span, which means the
    # botocore instrumentor isn't actually running. We require a provider
    # span to be sure capture works end-to-end.
)


# Cheapest possible prompt that still forces the agent to make at least one
# LLM call. Two-word prompts force a real generation; an empty string would
# let some agents short-circuit before calling Bedrock.
_CANARY_PROMPT = "Say hello."


@dataclass
class CanaryResult:
    """Outcome of the pre-flight check.

    `ok=True` means we saw at least one provider LLM span and the eval is
    safe to run. `ok=False` means the agent ran but produced no captured
    LLM spans (silent capture failure) OR the agent crashed.

    `error` carries a human-readable explanation. `agent_stderr` is the
    raw subprocess stderr when the agent itself failed to run, so the
    caller can surface it to the user without opening logs.
    """

    ok: bool
    error: Optional[str] = None
    agent_stderr: Optional[str] = None
    spans_seen: int = 0
    llm_spans_seen: int = 0


def _has_llm_span(spans: list[Any]) -> tuple[bool, int]:
    """Return (saw_llm_span, count_of_llm_spans).

    A span counts if its `gen_ai.system` attr is in our provider allowlist.
    We don't count spans that have only `gen_ai.request.model` set — those
    can be emitted by frameworks that didn't actually reach the wire (e.g.
    a Strands span emitted before botocore's converse call).
    """
    count = 0
    for s in spans:
        attrs = dict(getattr(s, "attributes", None) or {})
        system = attrs.get("gen_ai.system")
        if system in _LLM_SYSTEM_VALUES:
            count += 1
    return count > 0, count


def run_canary(
    *,
    agent_path: str,
    agent_entry: str,
    requirements_path: Optional[str] = None,
    venv_python: Optional[str] = None,
    timeout: float = 60.0,
) -> CanaryResult:
    """Spawn the agent once with a trivial prompt; verify ≥1 Bedrock span lands.

    Returns CanaryResult.ok=True when capture is working. ok=False with a
    diagnostic message otherwise. Always tears down the receiver before
    returning, even on errors, so we don't leak threads/ports.

    Cost: one LLM call to whatever model the agent is using, plus ~1s of
    receiver startup/teardown. Pays for itself the first time it catches
    a broken pipeline.
    """
    handle = start_receiver()
    try:
        try:
            run_agent_subprocess(
                agent_path=agent_path,
                agent_entry=agent_entry,
                prompt=_CANARY_PROMPT,
                otlp_endpoint=handle.url,
                sample_id="__canary__",
                requirements_path=requirements_path,
                venv_python=venv_python,
                timeout=timeout,
            )
        except AgentSubprocessError as e:
            # Agent itself crashed. The eval would crash on every sample
            # in the same way — abort now with the actual stderr so the
            # user can fix their agent before paying for N samples.
            return CanaryResult(
                ok=False,
                error=(
                    "Pre-flight check: the agent crashed on a 'hello' "
                    "prompt. The eval would fail on every sample the "
                    "same way. See agent_stderr for the actual error."
                ),
                agent_stderr=str(e),
            )

        spans, _logs = handle.drain()
        saw_llm, count = _has_llm_span(spans)

        if not saw_llm:
            return CanaryResult(
                ok=False,
                error=(
                    "Pre-flight check: agent ran successfully but no "
                    f"Bedrock LLM spans were captured ({len(spans)} "
                    "non-LLM spans seen). This usually means: "
                    "(1) the agent didn't call Bedrock, "
                    "(2) opentelemetry-instrumentation-botocore is not "
                    "installed in the agent venv, or "
                    "(3) the OTel instrumentor is incompatible with the "
                    "boto3 version. The eval would silently produce "
                    "empty scores."
                ),
                spans_seen=len(spans),
                llm_spans_seen=0,
            )

        return CanaryResult(ok=True, spans_seen=len(spans), llm_spans_seen=count)

    finally:
        handle.shutdown()
