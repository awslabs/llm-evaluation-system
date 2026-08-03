"""Runtime patches to Inspect AI, applied at import inside eval subprocesses.

Inspect's Bedrock provider injects a constant ``max_tokens`` when the caller
passes none: ``DEFAULT_MAX_TOKENS = 2048`` (``inspect_ai/_util/constants.py``),
selected by a hand-coded family table in ``_providers/bedrock.py``. On the
Converse API that overrides a correct AWS default — the API reference states
that when ``maxTokens`` is omitted, "the default value is the maximum allowed
value for the model that you are using". So Inspect turns "run to the model's
limit" into "stop at 2048", which for reasoning models (gpt-oss, GPT-5.x) is
worse than a short truncation: they can spend the whole 2048 on the reasoning
channel and return an EMPTY completion with ``stop_reason="max_tokens"`` that
scores 0 while the run reports success.

The fix is to stop Inspect injecting anything and let Bedrock apply its own
default. There is no clean per-model number to substitute instead — verified
against live Bedrock, LiteLLM's advertised limits are wrong for 35 of 38
on-demand models (too low for 29, dangerously too high for one: it lists
qwen3-235b at 131072 when Bedrock's real max is 65536, which is a hard
ValidationException, not a clamp). And no single constant works either: 8192 is
rejected by writer.palmyra-vision (max 4096) while being too low for gpt-oss.
Omitting sidesteps all of it — Bedrock picks a value the model accepts by
construction.

Why a monkeypatch and not a subclass: targets reach the eval as bare model
strings on ``inspect eval --model``, which routes through Inspect's own provider
registry — there's no seam to inject a subclass. The generated task file imports
this module so the patch lands inside the subprocess that actually calls the
model. ``run_eval``/``optimize_prompt``/``benchmarks`` also import it for the
CLI-level paths.

This is a workaround for an upstream gap. When Inspect's Bedrock provider stops
substituting a constant (or Bedrock starts clamping instead of rejecting),
delete this module and its imports.

NOTE — this does NOT make every model run to its true maximum. Bedrock's omitted
default is itself sometimes below the model's real ceiling (gpt-oss-20b defaults
to 4096 though it accepts 8192+). Omitting is strictly better than 2048 and
never crashes; the residual truncation is surfaced by the jury scorer's
``truncated_partial_output`` / ``truncated_no_output`` flags rather than guessed
around.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_applied = False


def apply_inspect_patches() -> None:
    """Idempotently apply all Inspect runtime patches. Safe to call repeatedly."""
    global _applied
    if _applied:
        return
    _patch_bedrock_default_max_tokens()
    _applied = True


def _patch_bedrock_default_max_tokens() -> None:
    """Make the Bedrock provider omit ``max_tokens`` so Bedrock's model-max
    default applies instead of Inspect's constant 2048."""
    try:
        from inspect_ai.model._providers.bedrock import BedrockAPI
    except Exception as exc:  # pragma: no cover - import shape changed upstream
        logger.warning(
            "Could not patch Inspect Bedrock max_tokens default (%s); reasoning "
            "models may return empty completions truncated at 2048.",
            exc,
        )
        return

    def _no_default_max_tokens(self) -> None:
        return None

    BedrockAPI.max_tokens = _no_default_max_tokens


# Apply on import so a generated task file gets the patch simply by importing
# this module — no call site to forget.
apply_inspect_patches()
