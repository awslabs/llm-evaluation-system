"""Tests for the create_config scorer-selection renderer.

Narrow deterministic-logic tests — the kind pytest is suited for per
``docs/DEVELOPMENT.md``. Verifies that the rendered task file matches
the requested scorer list, defaults stay byte-for-byte compatible, and
unknown scorer names raise.
"""

import ast

import pytest

from eval_mcp.core.judge_config import JudgeConfig
from eval_mcp.tools.create_config import (
    DEFAULT_SCORERS,
    SCORER_REGISTRY,
    _render_builtin_scorer_imports,
    _render_scorer_expression,
    _validate_scorers,
    create_inspect_task_file,
)


@pytest.fixture
def jc() -> JudgeConfig:
    return JudgeConfig(
        criteria=[{"name": "correct", "description": "1 if right, 0 if wrong"}],
        judges={"claude": "mockllm/model"},
    )


def _render(jc: JudgeConfig, scorers=None) -> tuple[str, dict]:
    return create_inspect_task_file(
        dataset_path="/tmp/ds.json",
        providers=["mockllm/model"],
        config_name="t",
        config_dir="/tmp",
        judge_config=jc,
        scorers=scorers,
    )


def test_default_is_jury(jc: JudgeConfig) -> None:
    code, cfg = _render(jc)
    assert "scorer=jury_scorer()" in code
    assert "def jury_scorer" in code
    assert cfg["scorers"] == ["jury"]


def test_default_task_file_is_valid_python(jc: JudgeConfig) -> None:
    code, _ = _render(jc)
    ast.parse(code)


def test_f1_only_skips_jury_block(jc: JudgeConfig) -> None:
    code, cfg = _render(jc, scorers=["f1"])
    assert "from inspect_ai.scorer import f1" in code
    assert "scorer=f1()" in code
    assert "def jury_scorer" not in code
    assert "JUDGE_MODELS" not in code
    assert "_build_scoring_tool" not in code
    assert cfg["scorers"] == ["f1"]
    ast.parse(code)


def test_composition_produces_list_scorer(jc: JudgeConfig) -> None:
    code, cfg = _render(jc, scorers=["jury", "f1"])
    assert "scorer=[jury_scorer(), f1()]" in code
    assert "def jury_scorer" in code
    assert "from inspect_ai.scorer import f1" in code
    assert cfg["scorers"] == ["jury", "f1"]
    ast.parse(code)


def test_all_builtins_compose(jc: JudgeConfig) -> None:
    code, cfg = _render(jc, scorers=["jury", "f1", "exact", "includes", "match"])
    assert "scorer=[jury_scorer(), f1(), exact(), includes(), match()]" in code
    assert "from inspect_ai.scorer import exact, f1, includes, match" in code
    ast.parse(code)


def test_dedupes_repeated_scorers(jc: JudgeConfig) -> None:
    _, cfg = _render(jc, scorers=["f1", "jury", "f1", "jury"])
    assert cfg["scorers"] == ["f1", "jury"]


def test_unknown_scorer_raises(jc: JudgeConfig) -> None:
    with pytest.raises(ValueError, match="Unknown scorer"):
        _validate_scorers(["not_a_scorer"])


def test_empty_list_falls_back_to_default() -> None:
    assert _validate_scorers([]) == list(DEFAULT_SCORERS)
    assert _validate_scorers(None) == list(DEFAULT_SCORERS)


def test_render_scorer_expression_single() -> None:
    assert _render_scorer_expression(["jury"]) == "jury_scorer()"
    assert _render_scorer_expression(["f1"]) == "f1()"


def test_render_scorer_expression_list() -> None:
    assert _render_scorer_expression(["jury", "f1"]) == "[jury_scorer(), f1()]"


def test_render_builtin_imports_excludes_jury() -> None:
    # jury defines its scorer inline; no import-from-inspect-scorer needed
    assert _render_builtin_scorer_imports(["jury"]) == ""


def test_render_builtin_imports_sorted_unique() -> None:
    line = _render_builtin_scorer_imports(["match", "f1", "exact", "jury"])
    assert line == "from inspect_ai.scorer import exact, f1, match"


def test_registry_keys_are_documented_set() -> None:
    # If new scorers are added, the tool docstring in server.py + the
    # plan need updating too — pin the set so additions surface in review.
    assert set(SCORER_REGISTRY.keys()) == {
        "jury",
        "f1",
        "exact",
        "includes",
        "match",
    }


def test_prompt_template_carries_scorer_expr(jc: JudgeConfig) -> None:
    # Prompt-comparison path: multiple prompts → multiple @task defs,
    # each must reference the chosen scorer expression.
    code, _ = create_inspect_task_file(
        dataset_path="/tmp/ds.json",
        providers=["mockllm/model"],
        config_name="t",
        config_dir="/tmp",
        judge_config=jc,
        prompts=["Prompt A: {question}", "Prompt B: {question}"],
        scorers=["f1"],
    )
    assert code.count("scorer=f1()") == 2
    assert "@task" in code
    assert "def eval_1" in code
    assert "def eval_2" in code
    ast.parse(code)


# ----- score-only mode -----


def _render_score_only(jc: JudgeConfig, scorers=None, prompts=None) -> tuple[str, dict]:
    return create_inspect_task_file(
        dataset_path="/tmp/ds.json",
        providers=[],
        config_name="t",
        config_dir="/tmp",
        judge_config=jc,
        scorers=scorers,
        prompts=prompts,
        score_only=True,
    )


def test_score_only_emits_static_solver(jc: JudgeConfig) -> None:
    code, cfg = _render_score_only(jc, scorers=["f1"])
    # Imports the solver from our module
    assert (
        "from eval_mcp.solvers.static_output import static_output_solver"
        in code
    )
    # Solver chain uses the static solver instead of generate()
    assert "solver=[static_output_solver()]" in code
    # No `generate()` call in the solver chain (the chain itself —
    # `generate` is still imported via the base template, used as a
    # fallback inside the solver factory itself).
    assert "solver=[generate()]" not in code
    # FieldSpec carries actual_output as metadata
    assert 'metadata=["actual_output"]' in code
    # Config has the score_only flag set
    assert cfg["score_only"] is True
    ast.parse(code)


def test_score_only_with_jury(jc: JudgeConfig) -> None:
    code, _ = _render_score_only(jc, scorers=["jury"])
    # Jury scorer block still emitted — jury grades the pre-generated answer
    assert "def jury_scorer" in code
    assert "scorer=jury_scorer()" in code
    # Static solver still wired in
    assert "solver=[static_output_solver()]" in code
    ast.parse(code)


def test_score_only_doc_mentions_mode(jc: JudgeConfig) -> None:
    code, _ = _render_score_only(jc, scorers=["f1"])
    assert "score-only" in code.lower()


def test_score_only_with_prompts(jc: JudgeConfig) -> None:
    code, _ = _render_score_only(
        jc, scorers=["f1"], prompts=["Prompt A: {question}", "Prompt B: {question}"]
    )
    # Both @task defs use the static solver
    assert code.count("static_output_solver()") == 2
    assert code.count('metadata=["actual_output"]') == 2
    assert "solver=[generate()" not in code
    ast.parse(code)


def test_non_score_only_unchanged(jc: JudgeConfig) -> None:
    # Sanity: backward-compat path. Default mode emits generate(), no
    # static solver, no metadata field, no score_only flag.
    code, cfg = _render(jc, scorers=["f1"])
    assert "solver=[generate()]" in code
    assert "static_output_solver" not in code
    assert 'metadata=["actual_output"]' not in code
    assert "score_only" not in cfg
