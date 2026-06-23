"""Narrow deterministic tests for the benchmark discovery/run helpers.

These cover the pure logic (filtering, the id-vs-task resolver, the compact
projection) — not the subprocess run, which needs Inspect + a model and is
exercised end-to-end from Claude Code per the repo's testing philosophy.
"""

import json

import pytest

from eval_mcp.tools.benchmarks import (
    _matches,
    _resolve_task,
    _entry_summary,
    handle_list_benchmarks,
    handle_get_benchmark_details,
    handle_run_benchmark,
)


class _Task:
    def __init__(self, name, samples=None):
        self.name = name
        self.dataset_samples = samples


class _Entry:
    """Minimal stand-in for an inspect_evals EvalListing entry."""

    def __init__(self, id, title="", description="", group=None, tasks=None,
                 dependency=None, dependency_group=None, isolated=False,
                 external_assets=None, arxiv=None):
        self.id = id
        self.title = title
        self.description = description
        self.group = group
        self.tasks = tasks or []
        self.dependency = dependency
        self.dependency_group = dependency_group
        self.isolated = isolated
        self.external_assets = external_assets or []
        self.arxiv = arxiv


def test_matches_search_hits_id_title_and_tasks():
    e = _Entry("gsm8k", title="Grade School Math", group="Mathematics",
               tasks=[_Task("gsm8k")])
    assert _matches(e, "grade", None)        # title
    assert _matches(e, "gsm8k", None)        # id / task
    assert _matches(e, "MATH", None)         # case-insensitive, group
    assert not _matches(e, "coding", None)


def test_matches_category_is_exact_case_insensitive():
    e = _Entry("x", group="Coding", tasks=[_Task("x")])
    assert _matches(e, None, "coding")
    assert _matches(e, None, "Coding")
    assert not _matches(e, None, "Code")


def test_entry_summary_flags_and_first_sample_count():
    e = _Entry("swe", title="SWE", group="Coding",
               tasks=[_Task("swe", None), _Task("swe_v", 500)],
               dependency_group="swe_bench", isolated=True)
    s = _entry_summary(e)
    assert s["needsExtra"] is True
    assert s["needsSandbox"] is True
    assert s["sampleCount"] == 500  # skips the None, takes first real count
    assert s["tasks"] == ["swe", "swe_v"]


def test_resolve_task_by_exact_task_name():
    evals = [_Entry("mmlu", tasks=[_Task("mmlu_0_shot"), _Task("mmlu_5_shot")])]
    r = _resolve_task("mmlu_5_shot", evals)
    assert r["ok"] and r["task"] == "mmlu_5_shot"


def test_resolve_task_by_single_task_id():
    evals = [_Entry("gsm8k", tasks=[_Task("gsm8k")])]
    r = _resolve_task("gsm8k", evals)
    assert r["ok"] and r["task"] == "gsm8k"


def test_resolve_task_multivariant_id_refused_with_variants_listed():
    evals = [_Entry("mmlu", tasks=[_Task("mmlu_0_shot"), _Task("mmlu_5_shot")])]
    r = _resolve_task("mmlu", evals)
    assert not r["ok"]
    assert "mmlu_0_shot" in r["error"] and "mmlu_5_shot" in r["error"]


def test_resolve_task_unknown():
    r = _resolve_task("does_not_exist", [_Entry("gsm8k", tasks=[_Task("gsm8k")])])
    assert not r["ok"] and "Unknown" in r["error"]


@pytest.mark.asyncio
async def test_run_benchmark_requires_providers():
    out = await handle_run_benchmark({"task": "gsm8k", "providers": [], "user_id": "u"})
    d = json.loads(out[0].text)
    assert d["success"] is False and "provider" in d["error"].lower()


@pytest.mark.asyncio
async def test_run_benchmark_requires_user_id():
    out = await handle_run_benchmark({"task": "gsm8k", "providers": ["mockllm/model"]})
    d = json.loads(out[0].text)
    assert d["success"] is False and "user_id" in d["error"]


@pytest.mark.asyncio
async def test_list_benchmarks_returns_catalog_and_categories():
    # Hits the real inspect_evals catalog (cheap YAML load).
    out = await handle_list_benchmarks({"limit": 5})
    d = json.loads(out[0].text)
    assert d["success"] is True
    assert d["totalCatalog"] > 100
    assert len(d["benchmarks"]) == 5
    assert "Knowledge" in d["categories"]


@pytest.mark.asyncio
async def test_get_benchmark_details_unknown_suggests_near_matches():
    out = await handle_get_benchmark_details({"benchmark_id": "gsm"})
    d = json.loads(out[0].text)
    assert d["success"] is False
    assert "gsm8k" in d["didYouMean"]
