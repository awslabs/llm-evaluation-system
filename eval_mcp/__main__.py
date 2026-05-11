"""Entry point for `python -m eval_mcp`.

Delegates to the Click CLI so all subcommands (view, serve, init, ...) work
when eval_mcp is invoked as a module. Mirrors the `eval-mcp` console script
but doesn't depend on PATH.
"""
from eval_mcp.cli import main

if __name__ == "__main__":
    main()
