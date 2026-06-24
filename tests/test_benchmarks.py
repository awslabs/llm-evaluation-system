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
                 external_assets=None, arxiv=None, runtime_metadata=None):
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
        self.runtime_metadata = runtime_metadata

    def model_dump(self):
        # _sandbox_requirement reads runtime_metadata via model_dump(); mirror
        # the real EvalListing entry's shape.
        return {"isolated": self.isolated, "runtime_metadata": self.runtime_metadata}


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


def test_sandbox_detected_from_runtime_metadata_not_isolated():
    """A code-execution benchmark flags needsSandbox via runtime_metadata.sandbox
    even when the legacy `isolated` flag is False (the HumanEval bug)."""
    from eval_mcp.tools.benchmarks import _sandbox_requirement
    he = _Entry("humaneval", group="Coding", tasks=[_Task("humaneval")],
                isolated=False,
                runtime_metadata={"sandbox": ["scorer"], "supports_k8s": False})
    sb = _sandbox_requirement(he)
    assert sb["needs"] is True
    assert sb["phases"] == ["scorer"]
    assert sb["supportsK8s"] is False
    # and the compact summary reflects it
    s = _entry_summary(he)
    assert s["needsSandbox"] is True
    assert s["sandboxSupportsK8s"] is False


def test_no_sandbox_for_plain_qa_benchmark():
    from eval_mcp.tools.benchmarks import _sandbox_requirement
    g = _Entry("gsm8k", group="Mathematics", tasks=[_Task("gsm8k")],
               runtime_metadata=None)
    assert _sandbox_requirement(g)["needs"] is False
    assert _entry_summary(g)["needsSandbox"] is False


@pytest.mark.asyncio
async def test_run_benchmark_injects_sandbox_for_code_eval():
    """A code-execution benchmark must get the resolved sandbox injected as
    `-T sandbox=<type>` (k8s on EKS) so its verify scorer can run — instead of
    being rejected. We intercept the subprocess to capture the launched cmd."""
    import os
    from unittest.mock import patch, AsyncMock
    from eval_mcp.tools import benchmarks as bm

    he = _Entry("humaneval", group="Coding", tasks=[_Task("humaneval")],
                runtime_metadata={"sandbox": ["scorer"], "supports_k8s": False})

    captured = {}

    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.wait = AsyncMock(return_value=0)
        proc.pid = 4242
        return proc

    with patch.object(bm, "_load_evals", return_value=[he]), \
         patch.object(bm, "raise_if_autodetect_error", lambda: None), \
         patch.object(bm.asyncio, "create_subprocess_exec", side_effect=fake_exec), \
         patch.dict(os.environ, {"INSPECT_SANDBOX_TYPE": "k8s"}, clear=False):
        await bm.handle_run_benchmark({
            "task": "humaneval", "user_id": "t", "providers": ["bedrock/x"],
        })

    cmd = captured.get("cmd", [])
    # The launched inspect command must carry -T sandbox=k8s.
    assert "-T" in cmd and "sandbox=k8s" in cmd, f"cmd missing sandbox inject: {cmd}"


@pytest.mark.asyncio
async def test_run_benchmark_no_sandbox_inject_for_plain_qa():
    """A non-sandbox benchmark (gsm8k) must NOT get a sandbox -T arg."""
    import os
    from unittest.mock import patch, AsyncMock
    from eval_mcp.tools import benchmarks as bm

    g = _Entry("gsm8k", group="Mathematics", tasks=[_Task("gsm8k")],
               runtime_metadata=None)
    captured = {}

    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.wait = AsyncMock(return_value=0)
        proc.pid = 4243
        return proc

    with patch.object(bm, "_load_evals", return_value=[g]), \
         patch.object(bm, "raise_if_autodetect_error", lambda: None), \
         patch.object(bm.asyncio, "create_subprocess_exec", side_effect=fake_exec), \
         patch.dict(os.environ, {"INSPECT_SANDBOX_TYPE": "k8s"}, clear=False):
        await bm.handle_run_benchmark({
            "task": "gsm8k", "user_id": "t", "providers": ["bedrock/x"],
        })

    cmd = captured.get("cmd", [])
    assert not any(c.startswith("sandbox=") for c in cmd), f"unexpected sandbox inject: {cmd}"


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
