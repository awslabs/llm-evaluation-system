"""Bootstrap script run inside the agent's subprocess.

Invoked as `python _agent_launcher.py <agent_path> <agent_entry> <prompt>`.
We deliberately do NOT wrap with `opentelemetry-instrument`: that CLI
prepends its sitecustomize dir to PYTHONPATH, which leaks to any
grandchild Python process (e.g. boto3's `credential_process` helpers
like `isengardcli` that bundle their own interpreter without OTel
installed). Instead we bootstrap auto-instrumentation in-process here,
then load the user's agent.

The launcher's job is narrow: turn on OTel, import the user's agent by
file path, call its entry function, and write the result to stdout
framed with a marker so the parent can extract it reliably even if the
agent printed log lines of its own.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys


_BEGIN = "__EVAL_RESULT__"
_END = "__EVAL_END__"


def _bootstrap_otel() -> None:
    """Run the same auto-instrumentation the CLI would, but in-process.

    `initialize()` is exactly what opentelemetry-distro's sitecustomize
    calls. Invoking it here means we never set PYTHONPATH to point at
    that sitecustomize, so grandchild processes (credential_process
    helpers, subprocess.run calls from the agent) inherit a clean env.
    """
    # Belt-and-suspenders: even if something upstream left PYTHONPATH
    # pointing at OTel's sitecustomize dir, drop it before the agent
    # runs so any grandchild python won't try to import opentelemetry.
    os.environ.pop("PYTHONPATH", None)
    from opentelemetry.instrumentation.auto_instrumentation import initialize
    initialize()


def _main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit(
            "usage: _agent_launcher.py <agent_path> <agent_entry> <prompt>"
        )
    agent_path, agent_entry, prompt = sys.argv[1], sys.argv[2], sys.argv[3]

    _bootstrap_otel()

    spec = importlib.util.spec_from_file_location("_user_agent", agent_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load agent module from {agent_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    fn = getattr(module, agent_entry, None)
    if fn is None:
        raise AttributeError(
            f"agent at {agent_path} has no '{agent_entry}' callable"
        )

    result = fn(prompt)

    # Force-flush OTel exporters before the process exits. The BatchSpanProcessor
    # flushes on shutdown via atexit, but for short-lived subprocesses the
    # exporter's HTTP request can race with interpreter teardown — we've seen
    # silent span loss without this. force_flush is synchronous and reliable.
    try:
        from opentelemetry import trace
        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis=10000)
    except Exception:
        pass
    try:
        from opentelemetry._logs import get_logger_provider
        provider = get_logger_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis=10000)
    except Exception:
        pass

    # Frame so the parent's stdout parse survives whatever the agent printed.
    sys.stdout.write(_BEGIN + json.dumps({"output": str(result)}) + _END + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    _main()
