"""Run an agent in an isolated subprocess + venv.

Pairs with eval_mcp.otlp_receiver: the runner spawns the agent under
`opentelemetry-instrument`, configures it to emit OTLP to the harness's
receiver, captures its stdout for the final answer, and surfaces stderr
on failure.

When `requirements_path` is provided, `uv run --with-requirements` builds
an ephemeral venv from it before invoking the agent — this is what makes
LangChain / Strands / Pydantic-AI agents safe to evaluate side-by-side
without ever installing their deps into the harness's venv.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Optional, Tuple


_LAUNCHER_PATH = str(Path(__file__).parent / "_agent_launcher.py")
_RESULT_RE = re.compile(r"__EVAL_RESULT__(.*?)__EVAL_END__", re.DOTALL)

# OTel packages every agent venv needs to (a) auto-instrument boto3 (and any
# other Bedrock-touching library) and (b) ship spans back via OTLP/HTTP.
# Injected via `uv run --with` so the agent's requirements.txt never has to
# mention telemetry — the eval framework guarantees its own observability infra.
_OTEL_AGENT_DEPS = (
    "opentelemetry-distro",
    "opentelemetry-exporter-otlp-proto-http",
    "opentelemetry-instrumentation-botocore",
)


class AgentSubprocessError(RuntimeError):
    """Raised when the agent subprocess exits non-zero or omits the result
    marker. The message includes the agent's stderr so the eval viewer can
    surface something actionable to the user.
    """


def build_command(
    *,
    agent_path: str,
    agent_entry: str,
    prompt: str,
    otlp_endpoint: str,
    sample_id: str,
    requirements_path: Optional[str] = None,
) -> Tuple[list[str], dict[str, str]]:
    """Assemble the argv + env for spawning one agent invocation.

    Split out as a pure function so its shape can be regression-tested
    without touching the filesystem or running a real process.
    """
    # Every agent invocation goes through `uv run --no-project` so it ALWAYS
    # gets an isolated ephemeral venv — there is no host-venv fallback path.
    # That guarantees the harness's installed packages are unreachable from
    # the agent, which is the only way to permanently rule out dep skew.
    uv_args: list[str] = ["uv", "run", "--no-project"]
    if requirements_path:
        uv_args += ["--with-requirements", requirements_path]
    for pkg in _OTEL_AGENT_DEPS:
        uv_args += ["--with", pkg]

    # `--` separates uv's args from the command it should run.
    # opentelemetry-instrument auto-loads the registered instrumentations
    # at startup so the agent's library calls (boto3.bedrock, etc.) emit
    # OTLP without the agent author writing any OTel code.
    argv = uv_args + [
        "--",
        "opentelemetry-instrument",
        "python",
        _LAUNCHER_PATH,
        agent_path,
        agent_entry,
        prompt,
    ]

    env = dict(os.environ)
    env.update({
        # Tell the OTel SDK in the agent process where to send batches. The
        # SDK appends /v1/traces and /v1/logs itself.
        "OTEL_EXPORTER_OTLP_ENDPOINT": otlp_endpoint,
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        # Required for the Bedrock instrumentation to emit message content
        # (input prompts, output text) as log records — without it we'd see
        # only span attrs (model name, token counts) but no actual messages.
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true",
        "OTEL_SERVICE_NAME": "user_agent",
        # Resource attribute the receiver uses to correlate spans back to
        # the eval sample they belong to.
        "OTEL_RESOURCE_ATTRIBUTES": f"eval.sample_id={sample_id}",
    })
    return argv, env


def run_agent_subprocess(
    *,
    agent_path: str,
    agent_entry: str,
    prompt: str,
    otlp_endpoint: str,
    sample_id: str,
    requirements_path: Optional[str] = None,
    timeout: float = 300.0,
) -> str:
    """Spawn the agent, wait for completion, return its answer string.

    Raises AgentSubprocessError on non-zero exit or missing result marker.
    Stderr is preserved in the exception message so the eval viewer can
    show the actual stack trace to whoever wrote the agent.
    """
    argv, env = build_command(
        agent_path=agent_path,
        agent_entry=agent_entry,
        prompt=prompt,
        otlp_endpoint=otlp_endpoint,
        sample_id=sample_id,
        requirements_path=requirements_path,
    )
    try:
        proc = subprocess.run(
            argv,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise AgentSubprocessError(
            f"Agent did not finish within {timeout}s"
        ) from e

    if proc.returncode != 0:
        raise AgentSubprocessError(
            f"Agent exited with code {proc.returncode}.\n"
            f"stderr:\n{proc.stderr.strip()}"
        )

    match = _RESULT_RE.search(proc.stdout)
    if not match:
        raise AgentSubprocessError(
            "Agent finished but did not emit a result marker — likely a "
            "launcher bug.\n"
            f"stdout:\n{proc.stdout.strip()}\n"
            f"stderr:\n{proc.stderr.strip()}"
        )
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as e:
        raise AgentSubprocessError(
            f"Could not parse agent result JSON: {e}\n"
            f"raw: {match.group(1)!r}"
        ) from e
    return payload["output"]
