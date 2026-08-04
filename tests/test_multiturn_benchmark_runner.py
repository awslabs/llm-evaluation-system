"""Runner-level tests for the bundled multi-turn benchmark tool.

Scope per CLAUDE.md: deterministic plumbing only — argument validation, the
judge fail-fast gate, and what lands on the launched inspect command line. The
subprocess and provider validation are intercepted; whether a jury actually
judges well is verified by running the benchmark for real.
"""

import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from eval_mcp.tools import multiturn_benchmarks as mt


def _fake_exec(captured):
    async def fake(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        proc = AsyncMock()
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        proc.pid = 4242
        return proc

    return fake


async def _run(args, captured, validated=None):
    async def fake_validate(providers):
        if validated is not None:
            validated.extend(providers)
        return {"valid": True}

    with patch.object(mt, "raise_if_autodetect_error", lambda: None), \
         patch.object(mt, "_validate_providers", side_effect=fake_validate), \
         patch.object(mt, "_refresh_keys_from_file", lambda: None), \
         patch.object(mt.asyncio, "create_subprocess_exec",
                      side_effect=_fake_exec(captured)):
        out = await mt.handle_run_multiturn_benchmark(
            {"task": "aiwf_medium_context", "user_id": "t", **args}
        )
    return json.loads(out[0].text)


@pytest.mark.asyncio
async def test_judge_models_lands_as_a_comma_joined_task_arg():
    captured = {}
    res = await _run(
        {"providers": ["bedrock/x"],
         "judge_models": ["bedrock/j1", "bedrock/j2"]},
        captured,
    )
    assert res["success"], res
    cmd = captured["cmd"]
    assert "judge_models=bedrock/j1,bedrock/j2" in cmd
    assert not any(c.startswith("judge_model=") for c in cmd)


@pytest.mark.asyncio
async def test_judge_models_accepts_a_comma_separated_string():
    captured = {}
    res = await _run(
        {"providers": ["bedrock/x"], "judge_models": "bedrock/j1, bedrock/j2"},
        captured,
    )
    assert res["success"], res
    assert "judge_models=bedrock/j1,bedrock/j2" in captured["cmd"]


@pytest.mark.asyncio
async def test_single_judge_model_still_lands_as_its_own_task_arg():
    captured = {}
    res = await _run(
        {"providers": ["bedrock/x"], "judge_model": "bedrock/j1"}, captured
    )
    assert res["success"], res
    assert "judge_model=bedrock/j1" in captured["cmd"]


@pytest.mark.asyncio
async def test_judge_model_and_judge_models_are_mutually_exclusive():
    captured = {}
    res = await _run(
        {"providers": ["bedrock/x"], "judge_model": "bedrock/j1",
         "judge_models": ["bedrock/j2"]},
        captured,
    )
    assert not res["success"]
    assert "not both" in res["error"]
    assert "cmd" not in captured  # rejected before any launch


@pytest.mark.asyncio
async def test_judges_are_validated_alongside_targets():
    """A typo'd judge must fail BEFORE the 30-turn conversation is paid for."""
    captured = {}
    validated = []
    res = await _run(
        {"providers": ["bedrock/x"],
         "judge_models": ["bedrock/j1", "bedrock/j2"]},
        captured,
        validated=validated,
    )
    assert res["success"], res
    assert set(validated) == {"bedrock/x", "bedrock/j1", "bedrock/j2"}


@pytest.mark.asyncio
async def test_judge_validation_failure_blocks_the_run():
    async def fail_validate(providers):
        return {"valid": False, "failed_providers": [
            {"provider": "bedrock/typo", "error": "no such model"}]}

    captured = {}
    with patch.object(mt, "raise_if_autodetect_error", lambda: None), \
         patch.object(mt, "_validate_providers", side_effect=fail_validate), \
         patch.object(mt.asyncio, "create_subprocess_exec",
                      side_effect=_fake_exec(captured)):
        out = await mt.handle_run_multiturn_benchmark({
            "task": "aiwf_medium_context", "user_id": "t",
            "providers": ["bedrock/x"], "judge_model": "bedrock/typo",
        })
    res = json.loads(out[0].text)
    assert not res["success"]
    assert "cmd" not in captured


@pytest.mark.asyncio
async def test_inspect_evals_branch_rejects_judge_params_loudly():
    """judge_model/judge_models/max_turns silently ignored on an inspect_evals
    task would let a caller believe an override took effect — must error."""
    from eval_mcp.tools import benchmarks as bm

    class _Task:
        def __init__(self, name):
            self.name = name
            self.dataset_samples = None

    class _Entry:
        def __init__(self, id, tasks):
            self.id = id
            self.title = self.description = ""
            self.group = "Mathematics"
            self.tasks = tasks
            self.dependency = self.dependency_group = None
            self.isolated = False
            self.external_assets = []
            self.arxiv = None

        def model_dump(self):
            return {"isolated": False, "runtime_metadata": None}

    g = _Entry("gsm8k", tasks=[_Task("gsm8k")])
    for key, value in (
        ("judge_model", "bedrock/j1"),
        ("judge_models", ["bedrock/j1", "bedrock/j2"]),
        ("max_turns", 5),
    ):
        with patch.object(bm, "_load_evals", return_value=[g]):
            out = await bm.handle_run_benchmark({
                "task": "gsm8k", "user_id": "t",
                "providers": ["bedrock/x"], key: value,
            })
        res = json.loads(out[0].text)
        assert not res["success"], key
        assert key in res["error"], res["error"]
