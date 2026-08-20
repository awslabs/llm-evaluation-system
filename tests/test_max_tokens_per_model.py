"""No eval hand-tunes max_tokens; the Converse patch discovers each model's max.

Inspect's Bedrock provider injects a constant `max_tokens` when the caller
passes none — `DEFAULT_MAX_TOKENS = 2048`. And Bedrock's own omitted-maxTokens
default is NOT "the maximum allowed value" as the AWS docs claim (measured
live: claude-sonnet-5 defaults to 4096). Either way, long completions and
reasoning truncate silently while the run reports success.

The fix (`eval_mcp.inspect_patches`, loaded inside the eval subprocess by
`eval_mcp/_inspect_main.py`): default to a deliberately oversized request
(`_PROBE_MAX_TOKENS`); Bedrock's validation error names the model's true
ceiling, which is cached and retried. No hand-maintained per-model table —
verified live, LiteLLM's advertised limits are wrong for 35 of 38 on-demand
models, so the API's own error message is the only trustworthy source.

Claude models normally bypass all of this: they route through Inspect's
native anthropic provider (eval_mcp/core/model_routing.py), which sizes
max_tokens itself.
"""
from __future__ import annotations

import ast
import inspect


def test_inspect_launcher_wraps_inspect_ai():
    """Evals must launch through the patched wrapper, not `-m inspect_ai`, or the
    Bedrock patch never reaches the subprocess that calls the model."""
    from eval_mcp.tools.run_eval import _INSPECT_CMD

    assert _INSPECT_CMD[-2:] == ["-m", "eval_mcp._inspect_main"], (
        f"_INSPECT_CMD must invoke the patched wrapper; got {_INSPECT_CMD}"
    )


def test_wrapper_imports_patches_before_delegating():
    """The wrapper's whole job: apply patches, then hand off to Inspect's CLI."""
    import eval_mcp._inspect_main as wrapper

    src = inspect.getsource(wrapper)
    assert "import eval_mcp.inspect_patches" in src
    assert "inspect_ai._cli.main" in src


def test_patch_defaults_to_probe_ceiling():
    """After the patch, an undiscovered model defaults to the oversized probe
    value — never Inspect's 2048 constant, never a silent small default.

    Guards the exact regression: a 2048 default here is what truncated real
    answers and emptied reasoning models.
    """
    import eval_mcp.inspect_patches as patches
    from inspect_ai.model import get_model

    for model_id in (
        "bedrock/us.amazon.nova-pro-v1:0",
        "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "bedrock/openai.gpt-oss-20b-1:0",
    ):
        api = get_model(model_id).api
        assert api.max_tokens() == patches._PROBE_MAX_TOKENS, (
            f"{model_id} did not default to the probe ceiling; the patch "
            f"didn't apply. Reasoning models will truncate/empty at 2048."
        )


def test_discovered_limit_replaces_probe():
    """Once Bedrock's validation error reveals a model's ceiling, max_tokens()
    must return exactly that ceiling for the model."""
    import eval_mcp.inspect_patches as patches
    from inspect_ai.model import get_model

    model_id = "us.amazon.nova-lite-v1:0"
    patches._discovered_max_tokens[model_id] = 12345
    try:
        assert get_model(f"bedrock/{model_id}").api.max_tokens() == 12345
    finally:
        patches._discovered_max_tokens.pop(model_id, None)


def test_patch_is_idempotent():
    """Imported by several modules and called explicitly; must be safe to repeat
    — a double-applied generate() wrapper would retry limit errors twice."""
    import eval_mcp.inspect_patches as patches
    from inspect_ai.model import get_model

    patches.apply_inspect_patches()
    patches.apply_inspect_patches()
    api = get_model("bedrock/us.amazon.nova-pro-v1:0").api
    assert api.max_tokens() == patches._PROBE_MAX_TOKENS
    assert api.generate.__name__ == "_generate_discovering_limit"


def test_no_eval_path_passes_a_global_max_tokens():
    """None of the three launch paths may build a `--max-tokens` argument — that
    would reintroduce a global ceiling and defeat the per-model default.

    Assert on parsed string literals so a mention in a comment doesn't trip it.
    """
    from eval_mcp.tools import benchmarks, optimize_prompt, run_eval

    for module in (run_eval, optimize_prompt, benchmarks):
        tree = ast.parse(inspect.getsource(module))
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert "--max-tokens" not in literals, (
            f"{module.__name__} builds a --max-tokens argument; evals must pass "
            f"none so Bedrock applies each model's own default"
        )


def test_generated_config_uses_plain_generate():
    """The task file generates with plain generate() — the ceiling is handled by
    the imported patch, not a custom solver. And it must import the patch so a
    directly-run generated config still gets it."""
    from eval_mcp.core.judge_config import JudgeConfig
    from eval_mcp.tools.create_config import create_inspect_task_file

    jc = JudgeConfig(criteria=[{"name": "acc", "description": "right?", "weight": 1.0}])
    code, _ = create_inspect_task_file(
        dataset_path="/tmp/x.json",
        providers=["bedrock/us.amazon.nova-pro-v1:0", "openai/bedrock/gpt-5.6-terra"],
        config_name="probe",
        config_dir="/tmp",
        judge_config=jc,
        prompts=["{question}"],
        scorers=["jury"],
    )
    assert "solver=[generate()]" in code
    assert "import eval_mcp.inspect_patches" in code


def test_generated_config_parses_in_every_mode():
    """The task file reaches evals as SOURCE TEXT, so a template break fails at
    eval time rather than in CI. Parse all three solver-chain variants."""
    from eval_mcp.core.judge_config import JudgeConfig
    from eval_mcp.tools.create_config import create_inspect_task_file

    jc = JudgeConfig(criteria=[{"name": "acc", "description": "right?", "weight": 1.0}])
    for kwargs in (
        {"scorers": ["jury"]},
        {"scorers": ["jury"], "score_only": True},
        {"scorers": ["faithfulness"]},
    ):
        code, _ = create_inspect_task_file(
            dataset_path="/tmp/x.json",
            providers=["bedrock/us.amazon.nova-pro-v1:0"],
            config_name="probe",
            config_dir="/tmp",
            judge_config=jc,
            prompts=["{question}"],
            **kwargs,
        )
        ast.parse(code)


def test_scorer_flags_both_truncation_shapes():
    """The truncation guard is the part that survives from the earlier design and
    the reason omitting is acceptable: it surfaces the residual cases (Bedrock's
    own omitted default is sometimes below a model's true max) instead of letting
    them score silently.

      - fully empty completion    -> truncated_no_output, scored 0 + explanation
      - answer cut off mid-stream -> truncated_partial_output, still scored but
        flagged as a floor

    The scorer lives in eval_mcp.scorers.jury (imported by generated configs;
    it was previously inlined as JURY_SCORER_BLOCK source text).
    """
    import inspect as _inspect

    from eval_mcp.scorers import jury as jury_module

    src = _inspect.getsource(jury_module)
    assert "truncated_no_output" in src
    assert "truncated_partial_output" in src


def test_pricing_no_longer_exposes_a_token_lookup():
    """The per-model lookup is gone — its numbers were wrong for most models and
    there's nothing left that should call it."""
    from eval_mcp.core import pricing

    assert not hasattr(pricing, "get_max_output_tokens"), (
        "get_max_output_tokens should have been removed with the per-model "
        "approach; the design no longer consults advertised limits"
    )
