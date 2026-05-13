"""Tests for the subprocess agent runner.

The runner is the bridge between the OTLP receiver and the agent's
isolated venv. Two responsibilities:

  - build_command(): pure — assemble argv + env. Tested in isolation.
  - run_agent_subprocess(): impure — exec the agent, parse its stdout,
    return its final answer. Tested with a fixture agent that uses only
    the standard library so the test doesn't depend on network / heavy deps.

OTel auto-instrumentation is configured via env vars — the runner sets the
standard OTEL_* envs that `opentelemetry-instrument` reads at agent startup.
Capturing those calls is verified separately by the receiver tests and the
end-to-end Phase 4 test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# build_command — pure
# ---------------------------------------------------------------------------


_REQUIRED_OTEL_AGENT_DEPS = (
    "opentelemetry-distro",
    "opentelemetry-exporter-otlp-proto-http",
    "opentelemetry-instrumentation-botocore",
)


def _has_with(argv: list[str], pkg: str) -> bool:
    """True if argv contains a `--with <pkg>` pair (in order)."""
    for i, a in enumerate(argv):
        if a == "--with" and i + 1 < len(argv) and argv[i + 1] == pkg:
            return True
    return False


def test_build_command_with_requirements_uses_uv_run():
    """Agent dirs that ship a requirements.txt get a dedicated venv via
    `uv run --with-requirements`. This is the path that makes dep skew
    impossible — the agent's langchain/strands/etc. never touch the
    harness's installed packages.

    OTel deps are injected via --with so the agent author doesn't have
    to remember telemetry packages in their requirements file.
    """
    from eval_mcp.subprocess_runner import build_command

    argv, env = build_command(
        agent_path="/tmp/agent.py",
        agent_entry="run_agent",
        prompt="What is 2+2?",
        otlp_endpoint="http://127.0.0.1:1234",
        sample_id="sample-7",
        requirements_path="/tmp/requirements.txt",
    )

    # uv run with --no-project so it ignores any pyproject.toml in cwd and
    # builds a clean ephemeral env.
    assert argv[0] == "uv"
    assert argv[1] == "run"
    assert "--no-project" in argv
    assert "--with-requirements" in argv
    assert "/tmp/requirements.txt" in argv
    # OTel agent-side deps must be injected — agent author shouldn't have
    # to know we're instrumenting them.
    for pkg in _REQUIRED_OTEL_AGENT_DEPS:
        assert _has_with(argv, pkg), f"missing --with {pkg}"
    # We deliberately do NOT wrap with `opentelemetry-instrument` — that CLI
    # prepends its sitecustomize dir to PYTHONPATH, which leaks to any
    # grandchild python (boto3 credential_process helpers etc.) and crashes
    # them with ModuleNotFoundError. The launcher bootstraps OTel in-process
    # instead; see eval_mcp/_agent_launcher.py.
    assert "opentelemetry-instrument" not in argv
    py_idx = argv.index("python")
    assert argv[py_idx] == "python"
    # The launcher takes (agent_path, agent_entry, prompt) as positional args.
    assert argv[-3:] == ["/tmp/agent.py", "run_agent", "What is 2+2?"]


def test_build_command_sets_standard_otel_envs():
    """Standard OTEL_* env vars are how `opentelemetry-instrument` and the
    OTel SDK discover the harness receiver. Pinning them here means we
    don't drift from the public OTLP spec.
    """
    from eval_mcp.subprocess_runner import build_command

    _, env = build_command(
        agent_path="/tmp/agent.py",
        agent_entry="run_agent",
        prompt="?",
        otlp_endpoint="http://127.0.0.1:1234",
        sample_id="sample-42",
        requirements_path=None,
    )

    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:1234"
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
    assert env["OTEL_TRACES_EXPORTER"] == "otlp"
    assert env["OTEL_LOGS_EXPORTER"] == "otlp"
    # Required for the Bedrock instrumentation to emit message content
    # (input prompts, output text) — without this we'd only see span attrs.
    assert env["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "true"
    # sample_id rides as a resource attribute so receiver-side correlation
    # can attribute spans to the right eval sample.
    assert "eval.sample_id=sample-42" in env["OTEL_RESOURCE_ATTRIBUTES"]
    assert env["OTEL_SERVICE_NAME"] == "user_agent"


def test_build_command_with_user_venv_uses_their_python_directly():
    """When the user has their own venv (with OTel installed), we invoke
    their python directly and let the launcher bootstrap OTel in-process.

    We deliberately skip the `opentelemetry-instrument` CLI wrapper: it
    mutates PYTHONPATH in a way that leaks to grandchild python processes
    (credential_process helpers like isengardcli bundle their own python
    that then crashes with ModuleNotFoundError). In-process bootstrap
    avoids that entire class of bug.
    """
    from eval_mcp.subprocess_runner import build_command

    argv, env = build_command(
        agent_path="/tmp/agent.py",
        agent_entry="run_agent",
        prompt="What is 2+2?",
        otlp_endpoint="http://127.0.0.1:1234",
        sample_id="s",
        venv_python="/path/to/their/.venv/bin/python",
    )

    # No uv ceremony at all — we invoke their venv's python directly.
    assert "uv" not in argv
    assert "--with-requirements" not in argv
    assert "--with" not in argv
    # No opentelemetry-instrument wrapper — launcher bootstraps OTel itself.
    assert "opentelemetry-instrument" not in " ".join(argv)
    # First binary is their venv's python, then launcher + args.
    assert argv[0] == "/path/to/their/.venv/bin/python"
    # The launcher takes (agent_path, agent_entry, prompt) as positional args.
    assert argv[-3:] == ["/tmp/agent.py", "run_agent", "What is 2+2?"]
    # OTel env vars are the same regardless of mode — receiver doesn't care.
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:1234"
    # PYTHONPATH must be stripped so nothing leaks into the agent or its
    # grandchildren. The launcher bootstraps OTel from a clean state.
    assert "PYTHONPATH" not in env


def test_build_command_user_venv_takes_priority_over_requirements():
    """If a venv_python is given AND a requirements_path, the user-venv
    mode wins — their working setup is the truth, our uv-managed mirror
    is just a fallback for users who don't have a working venv yet.
    """
    from eval_mcp.subprocess_runner import build_command

    argv, _ = build_command(
        agent_path="/tmp/agent.py",
        agent_entry="run_agent",
        prompt="?",
        otlp_endpoint="http://x",
        sample_id="s",
        venv_python="/path/to/their/.venv/bin/python",
        requirements_path="/tmp/requirements.txt",  # should be ignored
    )

    assert argv[0] == "/path/to/their/.venv/bin/python"
    assert "uv" not in argv


def test_build_command_without_requirements_still_uses_uv_run():
    """Even when the agent has no extra deps, we ALWAYS spawn via uv run
    so the agent venv is isolated from the harness venv. This is the
    dep-skew immunity guarantee — there is no host-venv fallback path.

    The shape differs from the with-requirements case in exactly one way:
    no --with-requirements flag.
    """
    from eval_mcp.subprocess_runner import build_command

    argv, _ = build_command(
        agent_path="/tmp/agent.py",
        agent_entry="run_agent",
        prompt="?",
        otlp_endpoint="http://x",
        sample_id="s",
        requirements_path=None,
    )

    assert argv[0] == "uv"
    assert argv[1] == "run"
    assert "--no-project" in argv
    assert "--with-requirements" not in argv
    # OTel deps still injected — the isolation guarantee includes telemetry.
    for pkg in _REQUIRED_OTEL_AGENT_DEPS:
        assert _has_with(argv, pkg), f"missing --with {pkg}"


# ---------------------------------------------------------------------------
# run_agent_subprocess — integration with a stdlib-only fixture agent
# ---------------------------------------------------------------------------


_FIXTURE_AGENT = """\
\"\"\"Stdlib-only test agent. No external deps, no Bedrock calls — just
echoes the prompt back transformed. Used to verify the subprocess runner's
spawn + stdout-parsing without needing AWS creds or network.\"\"\"

def run_agent(prompt: str) -> str:
    return f"echoed: {prompt}"
"""


def test_run_agent_subprocess_returns_agent_output(tmp_path: Path):
    """Integration: spawn the fixture agent via the runner (no requirements,
    so no uv-managed venv), confirm we get its return value back as a
    string. Validates the full spawn-and-parse round-trip.
    """
    from eval_mcp.subprocess_runner import run_agent_subprocess

    agent_file = tmp_path / "agent.py"
    agent_file.write_text(_FIXTURE_AGENT)

    output = run_agent_subprocess(
        agent_path=str(agent_file),
        agent_entry="run_agent",
        prompt="hello",
        otlp_endpoint="http://127.0.0.1:1",  # nothing will reach this; the agent makes no calls
        sample_id="t1",
        requirements_path=None,
        timeout=30,
    )

    assert output == "echoed: hello"


def test_run_agent_subprocess_raises_on_nonzero_exit(tmp_path: Path):
    """If the agent's entry point raises, the runner must surface a clear
    error with stderr context — not silently return an empty string.
    """
    from eval_mcp.subprocess_runner import AgentSubprocessError, run_agent_subprocess

    agent_file = tmp_path / "agent.py"
    agent_file.write_text(
        'def run_agent(prompt: str) -> str:\n'
        '    raise ValueError("agent blew up on purpose")\n'
    )

    with pytest.raises(AgentSubprocessError) as exc_info:
        run_agent_subprocess(
            agent_path=str(agent_file),
            agent_entry="run_agent",
            prompt="?",
            otlp_endpoint="http://127.0.0.1:1",
            sample_id="t2",
            requirements_path=None,
            timeout=30,
        )
    # The error message must include the agent's stderr so the user has
    # something actionable in the eval viewer.
    assert "agent blew up on purpose" in str(exc_info.value)


def test_agent_subprocess_does_not_leak_pythonpath_to_grandchildren(tmp_path: Path):
    """Regression guard for the isengardcli class of bug.

    `opentelemetry-instrument` (the CLI we used to wrap the agent with)
    prepends its sitecustomize dir to PYTHONPATH. Any grandchild python
    spawned by the agent — e.g. boto3's `credential_process` invoking an
    external binary that bundles its own interpreter — would inherit that
    PYTHONPATH, try to `import opentelemetry`, fail, and crash the parent.

    We now bootstrap OTel in-process inside _agent_launcher.py and strip
    PYTHONPATH before spawning the agent, so grandchildren see a clean env.
    This test pins that behavior: the agent spawns a subprocess and inspects
    the environment visible to it. If PYTHONPATH contains anything mentioning
    opentelemetry, we've regressed.
    """
    from eval_mcp.subprocess_runner import run_agent_subprocess

    agent_file = tmp_path / "agent.py"
    agent_file.write_text(
        'import os, subprocess, sys\n'
        'def run_agent(prompt):\n'
        '    # Grandchild: same python, but a fresh invocation. Print the\n'
        '    # PYTHONPATH it sees so the test can assert on it.\n'
        '    result = subprocess.run(\n'
        '        [sys.executable, "-c", "import os; print(os.environ.get(\\"PYTHONPATH\\", \\"\\"))"],\n'
        '        capture_output=True, text=True, timeout=15,\n'
        '    )\n'
        '    return f"GRANDCHILD_PYTHONPATH={result.stdout.strip()}"\n'
    )

    output = run_agent_subprocess(
        agent_path=str(agent_file),
        agent_entry="run_agent",
        prompt="?",
        otlp_endpoint="http://127.0.0.1:1",
        sample_id="leak-check",
        requirements_path=None,
        timeout=60,
    )

    assert output.startswith("GRANDCHILD_PYTHONPATH=")
    grandchild_pp = output.split("=", 1)[1]

    # The specific failure we're guarding: the OTel auto-instrumentation
    # sitecustomize dir must not be on the grandchild's PYTHONPATH. If it
    # is, spawning a python without OTel installed would crash in sitecustomize.
    assert "opentelemetry" not in grandchild_pp.lower(), (
        f"PYTHONPATH leaked OTel instrumentation dir to grandchild: {grandchild_pp!r}"
    )


def test_run_agent_subprocess_handles_non_string_return(tmp_path: Path):
    """Agents can return ints, dicts, anything. The runner serializes the
    return value as string (matching the in-process path in
    create_pipeline_eval_config._generate_local_task_code which does
    `str(_agent_fn(...))`).
    """
    from eval_mcp.subprocess_runner import run_agent_subprocess

    agent_file = tmp_path / "agent.py"
    agent_file.write_text("def run_agent(prompt): return 42\n")

    output = run_agent_subprocess(
        agent_path=str(agent_file),
        agent_entry="run_agent",
        prompt="?",
        otlp_endpoint="http://127.0.0.1:1",
        sample_id="t3",
        requirements_path=None,
        timeout=30,
    )
    assert output == "42"
