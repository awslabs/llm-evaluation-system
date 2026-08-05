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
    assert 'scorer=jury_scorer(CONFIG["criteria"], CONFIG["judge_models"], CONFIG["system_prompt"], CONFIG.get("mantle_regions"))' in code
    assert "from eval_mcp.scorers.jury import jury_scorer" in code
    assert cfg["scorers"] == ["jury"]


def test_default_task_file_is_valid_python(jc: JudgeConfig) -> None:
    code, _ = _render(jc)
    ast.parse(code)


def test_f1_only_skips_jury_block(jc: JudgeConfig) -> None:
    code, cfg = _render(jc, scorers=["f1"])
    assert "from inspect_ai.scorer import f1" in code
    assert "scorer=f1()" in code
    assert "jury_scorer" not in code
    assert cfg["scorers"] == ["f1"]
    ast.parse(code)


def test_composition_produces_list_scorer(jc: JudgeConfig) -> None:
    code, cfg = _render(jc, scorers=["jury", "f1"])
    assert 'scorer=[jury_scorer(CONFIG["criteria"], CONFIG["judge_models"], CONFIG["system_prompt"], CONFIG.get("mantle_regions")), f1()]' in code
    assert "from eval_mcp.scorers.jury import jury_scorer" in code
    assert "from inspect_ai.scorer import f1" in code
    assert cfg["scorers"] == ["jury", "f1"]
    ast.parse(code)


def test_all_builtins_compose(jc: JudgeConfig) -> None:
    code, cfg = _render(jc, scorers=["jury", "f1", "exact", "includes", "match"])
    assert 'scorer=[jury_scorer(CONFIG["criteria"], CONFIG["judge_models"], CONFIG["system_prompt"], CONFIG.get("mantle_regions")), f1(), exact(), includes(), match()]' in code
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
    assert _render_scorer_expression(["jury"]) == 'jury_scorer(CONFIG["criteria"], CONFIG["judge_models"], CONFIG["system_prompt"], CONFIG.get("mantle_regions"))'
    assert _render_scorer_expression(["f1"]) == "f1()"


def test_render_scorer_expression_list() -> None:
    assert _render_scorer_expression(["jury", "f1"]) == '[jury_scorer(CONFIG["criteria"], CONFIG["judge_models"], CONFIG["system_prompt"], CONFIG.get("mantle_regions")), f1()]'


def test_render_builtin_imports_includes_jury_module() -> None:
    # The jury imports from eval_mcp.scorers.jury like the RAG suite does.
    assert _render_builtin_scorer_imports(["jury"]) == (
        "from eval_mcp.scorers.jury import jury_scorer"
    )


def test_render_builtin_imports_sorted_unique() -> None:
    line = _render_builtin_scorer_imports(["match", "f1", "exact", "jury"])
    assert line == (
        "from eval_mcp.scorers.jury import jury_scorer\n"
        "from inspect_ai.scorer import exact, f1, match"
    )


def test_registry_keys_are_documented_set() -> None:
    # If new scorers are added, the tool docstring in server.py + the
    # plan need updating too — pin the set so additions surface in review.
    assert set(SCORER_REGISTRY.keys()) == {
        "jury",
        "f1",
        "exact",
        "includes",
        "match",
        # RAG suite (DeepEval QAG ports)
        "faithfulness",
        "answer_relevancy",
        "contextual_precision",
        "contextual_recall",
        "contextual_relevancy",
    }


def test_rag_scorer_renders_solver_and_metadata(jc: JudgeConfig) -> None:
    code, cfg = _render(jc, scorers=["faithfulness"])
    # Imports the scorer from our module + the judge-configure helper
    assert "from eval_mcp.scorers.rag import faithfulness" in code
    assert (
        "from eval_mcp.scorers.rag import configure_judge as _rag_configure_judge"
        in code
    )
    # FieldSpec carries retrieval_context onto Sample.metadata
    assert 'metadata=["retrieval_context"]' in code
    # Solver chain prepends the RAG solver before plain generate(). The
    # per-model token ceiling is handled by importing eval_mcp.inspect_patches
    # (Bedrock omits max_tokens), not by a custom solver.
    assert "solver=[rag_prompt_solver(), generate()]" in code
    # Wires the judge model at task-file import time
    assert "_rag_configure_judge(next(iter(CONFIG[\"judge_models\"].values())))" in code
    # Scorer call site
    assert "scorer=faithfulness()" in code
    assert cfg["scorers"] == ["faithfulness"]
    ast.parse(code)


def test_all_rag_scorers_compose_with_jury(jc: JudgeConfig) -> None:
    scorers = [
        "jury",
        "faithfulness",
        "answer_relevancy",
        "contextual_precision",
        "contextual_recall",
        "contextual_relevancy",
    ]
    code, cfg = _render(jc, scorers=scorers)
    assert (
        'scorer=[jury_scorer(CONFIG["criteria"], CONFIG["judge_models"], CONFIG["system_prompt"], CONFIG.get("mantle_regions")), faithfulness(), answer_relevancy(), '
        "contextual_precision(), contextual_recall(), "
        "contextual_relevancy()]"
    ) in code
    # All RAG scorers reach the right import line
    expected_import = (
        "from eval_mcp.scorers.rag import "
        "answer_relevancy, contextual_precision, contextual_recall, "
        "contextual_relevancy, faithfulness"
    )
    assert expected_import in code
    assert "from eval_mcp.scorers.jury import jury_scorer" in code
    assert cfg["scorers"] == scorers
    ast.parse(code)


def test_no_rag_init_when_no_rag_scorer(jc: JudgeConfig) -> None:
    code, _ = _render(jc, scorers=["jury", "f1"])
    assert "_rag_configure_judge" not in code
    assert "rag_prompt_solver" not in code
    assert 'metadata=["retrieval_context"]' not in code
    ast.parse(code)


def test_rag_scorer_in_prompt_comparison(jc: JudgeConfig) -> None:
    code, _ = create_inspect_task_file(
        dataset_path="/tmp/ds.json",
        providers=["mockllm/model"],
        config_name="t",
        config_dir="/tmp",
        judge_config=jc,
        prompts=["Prompt A: {question}", "Prompt B: {question}"],
        scorers=["faithfulness"],
    )
    # Each @task variant carries both the RAG solver and the metadata spec
    assert code.count("solver=[prompt_template") == 2
    assert code.count("rag_prompt_solver()") == 2
    assert code.count('metadata=["retrieval_context"]') == 2
    assert code.count("scorer=faithfulness()") == 2
    ast.parse(code)


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
    # Jury still wired in — it grades the pre-generated answer
    assert "from eval_mcp.scorers.jury import jury_scorer" in code
    assert 'scorer=jury_scorer(CONFIG["criteria"], CONFIG["judge_models"], CONFIG["system_prompt"], CONFIG.get("mantle_regions"))' in code
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
    # Sanity: backward-compat path. Default mode uses plain generate() — no
    # static solver, no metadata field, no score_only flag. The token ceiling is
    # handled by importing eval_mcp.inspect_patches (Bedrock omits max_tokens so
    # the model's own default applies instead of Inspect's constant 2048), not by
    # a custom solver.
    code, cfg = _render(jc, scorers=["f1"])
    assert "solver=[generate()]" in code
    assert "import eval_mcp.inspect_patches" in code
    assert "static_output_solver" not in code
    assert 'metadata=["actual_output"]' not in code
    assert "score_only" not in cfg


# ---------------------------------------------------------------------------
# Reasoning-model truncation
#
# Inspect's Bedrock provider defaults to max_tokens=2048. Reasoning models
# (gpt-5.6-luna/sol/terra, gpt-oss-*) can spend that ENTIRE budget on their
# reasoning channel and return an empty completion with
# stop_reason="max_tokens". Verified live: luna and sol both burned 2048/2048
# reasoning tokens with zero visible output on a hard proof task.
#
# The sample still "completes", so it used to score a plain 0.0 — a measurement
# error indistinguishable from a wrong answer. In a real comparison this cost
# gpt-oss-20b 3 samples and flipped it from 1st to last place.
# ---------------------------------------------------------------------------


def test_jury_scorer_flags_truncation_not_quality():
    """The jury scorer must distinguish truncation from a bad answer.

    It used to be emitted as source text (JURY_SCORER_BLOCK); it now lives in
    eval_mcp.scorers.jury, which generated configs import. Same invariants,
    pinned against the module source.
    """
    import inspect as _inspect

    from eval_mcp.scorers import jury as jury_module

    src = _inspect.getsource(jury_module)
    assert "TRUNCATED" in src
    assert "truncated_no_output" in src
    # It must key off stop_reason, not just the empty string.
    assert "stop_reason" in src
    # And still handle plain no-output separately.
    assert "No output generated" in src


def test_generated_config_imports_the_jury_module():
    """A freshly generated jury config must import the scorer from
    eval_mcp.scorers.jury and wire the CONFIG fields into it — the scorer no
    longer ships as inline source, so a bad import line breaks every eval."""
    import ast

    from eval_mcp.core.judge_config import JudgeConfig
    from eval_mcp.tools.create_config import create_inspect_task_file

    code, _ = create_inspect_task_file(
        dataset_path="/tmp/x.json",
        providers=["bedrock/us.amazon.nova-pro-v1:0"],
        config_name="probe",
        config_dir="/tmp",
        judge_config=JudgeConfig(),
        scorers=["jury"],
    )
    ast.parse(code)
    assert "from eval_mcp.scorers.jury import jury_scorer" in code
    assert 'jury_scorer(CONFIG["criteria"], CONFIG["judge_models"], ' in code
    assert 'CONFIG["system_prompt"], CONFIG.get("mantle_regions"))' in code
