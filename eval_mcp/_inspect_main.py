"""Entry point equivalent to ``python -m inspect_ai``, with our patches applied.

Eval subprocesses are launched as ``python -m eval_mcp._inspect_main eval ...``
instead of ``python -m inspect_ai eval ...`` so that
``eval_mcp.inspect_patches`` is imported *before* Inspect's CLI runs, inside the
subprocess that actually calls the model. This is the one place that covers
every eval path — generated tasks, benchmarks' upstream ``inspect_evals/*``
tasks, and retries — without each having to remember to import the patch.

Delegates to Inspect's real CLI entry (``inspect_ai._cli.main.main``), so it
stays a thin shim: no argument parsing here, forward argv untouched.
"""

from __future__ import annotations

import eval_mcp.inspect_patches  # noqa: F401  (applies patches on import)


def main() -> None:
    from inspect_ai._cli.main import main as inspect_main

    inspect_main()


if __name__ == "__main__":
    main()
