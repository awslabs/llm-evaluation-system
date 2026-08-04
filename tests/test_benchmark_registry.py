"""Tests for bundled-benchmark auto-discovery and the unified tool surface.

The contract these pin: adding a benchmark is dropping a directory in, and
callers reach both catalogs through one set of tools. Both are easy to
accidentally regress — the discovery by hardcoding a benchmark somewhere, the
unified surface by splitting the tools apart again.
"""

import json

import pytest
import yaml

from eval_mcp.benchmarks import registry


def test_discovers_the_bundled_benchmarks():
    found = registry.discover()
    assert "aiwf" in found, f"expected aiwf, discovered {list(found)}"


def test_discovery_is_driven_by_eval_yaml_not_python():
    """No benchmark may be named in the registry or the MCP tools.

    The whole point of eval.yaml discovery is that a new benchmark needs no code
    change. If an id leaks into these modules, the next port will need one.
    """
    from pathlib import Path

    for module in (
        Path(registry.__file__),
        Path(registry.__file__).parent.parent / "tools" / "multiturn_benchmarks.py",
    ):
        src = module.read_text(encoding="utf-8")
        for bench_id in registry.discover():
            # Allow it in comments/docstrings, not in code.
            code = "\n".join(
                line for line in src.splitlines()
                if not line.lstrip().startswith("#")
            )
            assert f'"{bench_id}"' not in code and f"'{bench_id}'" not in code, (
                f"{module.name} references benchmark id {bench_id!r} in code. "
                f"Discovery must be data-driven so a new benchmark needs no "
                f"Python edit."
            )


def test_every_declared_task_exists_in_task_py():
    """A name in eval.yaml with no matching @task fails only at run time."""
    for bench in registry.discover().values():
        src = bench.task_file.read_text(encoding="utf-8")
        for name in bench.task_names:
            assert f"def {name}(" in src, (
                f"{bench.id}: eval.yaml declares task {name!r} but "
                f"{bench.task_file.name} has no such function"
            )


def test_task_file_resolves_absolute():
    """Inspect is invoked as <abs path>@<task>; the eval subprocess runs in the
    user's storage dir, so a relative path would break."""
    for bench in registry.discover().values():
        assert bench.task_file.is_absolute()
        assert bench.task_file.is_file()


def test_resolve_accepts_a_task_name():
    hit = registry.resolve("aiwf_medium_context")
    assert hit is not None
    bench, task = hit
    assert bench.id == "aiwf"
    assert task == "aiwf_medium_context"


def test_resolve_rejects_a_multi_task_id():
    """An ambiguous id must not silently pick a variant."""
    bench = registry.discover()["aiwf"]
    assert len(bench.task_names) > 1
    assert registry.resolve("aiwf") is None


def test_resolve_returns_none_for_unknown():
    assert registry.resolve("definitely_not_a_benchmark") is None


def test_every_benchmark_declares_metrics_and_a_headline():
    for bench in registry.discover().values():
        assert bench.metrics, f"{bench.id}: eval.yaml declares no metrics"
        assert bench.headline_metric, f"{bench.id}: no headline metric"


def test_judge_scored_benchmarks_declare_a_default_judge():
    for bench in registry.discover().values():
        if not bench.judge:
            continue
        assert bench.default_judge, (
            f"{bench.id}: judge block present but no default. The judge is part "
            f"of the measurement; it can't be implicit."
        )


def test_eval_yaml_declares_per_task_cost():
    """Cost must be visible before a run — some of these are millions of tokens."""
    for bench in registry.discover().values():
        for t in bench.tasks:
            assert t.approx_input_tokens_per_model, (
                f"{bench.id}/{t.name}: eval.yaml must state "
                f"approx_input_tokens_per_model so callers aren't surprised."
            )


def test_eval_yaml_is_parseable_and_has_required_keys():
    for bench in registry.discover().values():
        raw = yaml.safe_load((bench.path / "eval.yaml").read_text(encoding="utf-8"))
        for key in ("title", "description", "group", "tasks", "source"):
            assert key in raw, f"{bench.id}: eval.yaml missing {key!r}"


def test_details_payload_is_json_serialisable():
    """It goes straight out over MCP as JSON."""
    for bench in registry.discover().values():
        json.dumps(bench.details())
        json.dumps(bench.summary())


# --- Unified tool surface -------------------------------------------------


@pytest.mark.asyncio
async def test_list_benchmarks_includes_bundled_and_upstream():
    from eval_mcp.tools.benchmarks import handle_list_benchmarks

    payload = json.loads((await handle_list_benchmarks({"limit": 200}))[0].text)
    ids = [b["id"] for b in payload["benchmarks"]]
    assert "aiwf" in ids, "bundled benchmark missing from list_benchmarks"
    assert "gsm8k" in ids, "upstream catalog missing from list_benchmarks"
    # Bundled sort first — cheapest to run, no external deps.
    assert payload["benchmarks"][0].get("bundled") is True


@pytest.mark.asyncio
async def test_list_benchmarks_search_finds_bundled():
    from eval_mcp.tools.benchmarks import handle_list_benchmarks

    payload = json.loads(
        (await handle_list_benchmarks({"search": "conversation"}))[0].text
    )
    assert "aiwf" in [b["id"] for b in payload["benchmarks"]]


@pytest.mark.asyncio
async def test_get_benchmark_details_resolves_bundled_by_id_and_task():
    from eval_mcp.tools.benchmarks import handle_get_benchmark_details

    for key in ("aiwf", "aiwf_long_context"):
        payload = json.loads((await handle_get_benchmark_details({"benchmark_id": key}))[0].text)
        assert payload["success"] and payload["id"] == "aiwf", key
        assert payload["bundled"] is True
        assert payload["caveats"], "bundled details must carry caveats"


@pytest.mark.asyncio
async def test_get_benchmark_details_still_resolves_upstream():
    from eval_mcp.tools.benchmarks import handle_get_benchmark_details

    payload = json.loads((await handle_get_benchmark_details({"benchmark_id": "gsm8k"}))[0].text)
    assert payload["success"] and payload["id"] == "gsm8k"
    assert not payload.get("bundled", False)


@pytest.mark.asyncio
async def test_run_benchmark_rejects_missing_providers_for_bundled():
    """Dispatch reaches the bundled runner's validation, not upstream's."""
    from eval_mcp.tools.benchmarks import handle_run_benchmark

    payload = json.loads(
        (await handle_run_benchmark({"task": "aiwf_medium_context", "user_id": "t"}))[0].text
    )
    assert payload["success"] is False
    assert "provider" in payload["error"].lower()
