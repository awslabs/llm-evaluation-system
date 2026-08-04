"""Bundled multi-turn benchmarks that ship with the MCP.

Distinct from ``eval_mcp/tools/benchmarks.py``, which is a thin wrapper over the
installed ``inspect_evals`` catalog. These are Inspect tasks we vendor and run
ourselves, for benchmark shapes ``inspect_evals`` doesn't cover.

Why they live here rather than upstream: as of 2026-05-08 ``inspect_evals``
stopped accepting new eval code (see its EVAL_REGISTER.md — a dependency-
isolation decision, not a quality one). New evals are expected to live in their
author's own repo and be *listed* upstream via the register. Register entries
aren't shipped in the ``inspect_evals`` wheel (``load_listing()`` resolves the
register dir as ``package_dir.parent.parent / "register"``, a source-checkout
path), so nothing in the register is runnable through ``run_benchmark``. Hence:
bundled here, launched by absolute path.
"""
