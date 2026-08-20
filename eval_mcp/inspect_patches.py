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
import re

logger = logging.getLogger(__name__)

_applied = False


def apply_inspect_patches() -> None:
    """Idempotently apply all Inspect runtime patches. Safe to call repeatedly."""
    global _applied
    if _applied:
        return
    _patch_bedrock_default_max_tokens()
    _patch_bedrock_gpt5_reasoning_effort()
    _patch_bedrock_redacted_reasoning()
    _patch_bedrock_read_timeout()
    _patch_anthropic_max_effort_ceiling()
    _applied = True


def _patch_bedrock_read_timeout() -> None:
    """Raise the Bedrock provider's 60s HTTP read timeout for reasoning runs.

    Converse calls are non-streaming: zero bytes flow while a model thinks,
    and max-effort GPT-5.x on hard problems reasons for minutes — verified
    live, 5/10 AIME samples died on ReadTimeoutError at the default 60s.
    Bump the default to 30 minutes; an explicit ``read_timeout`` model arg
    still wins. Remove if upstream moves Converse to streaming.
    """
    try:
        from inspect_ai.model._providers.bedrock import BedrockAPI
    except Exception as exc:  # pragma: no cover - import shape changed upstream
        logger.warning("Could not patch Bedrock read timeout (%s).", exc)
        return

    original_init = BedrockAPI.__init__

    def _init_with_generous_timeout(self, *init_args, **model_args):
        explicit = "read_timeout" in model_args
        original_init(self, *init_args, **model_args)
        if not explicit:
            self.read_timeout = 1800

    BedrockAPI.__init__ = _init_with_generous_timeout


def _patch_anthropic_max_effort_ceiling() -> None:
    """Request the model's true output ceiling at max/xhigh effort.

    Inspect's native anthropic provider sizes max_tokens as 32k base + 32k
    for max/xhigh effort = 64k — half of what Claude 4.6+/5 actually allow
    (128k), even though the same function clamps against the correct family
    caps a few lines later. On hard problems, max-effort thinking exhausts
    64k mid-reasoning and the sample silently scores 0 (verified live: 4/10
    AIME hard-tail samples). Bump the request to the 128k family ceiling for
    the models that have it, using the provider's own version predicates —
    no model-name table. Inspect auto-streams at >=8192 tokens, so large
    ceilings are safe. Remove when upstream requests its own cap.
    """
    try:
        from inspect_ai.model._providers.anthropic import AnthropicAPI
    except Exception as exc:  # pragma: no cover - import shape changed upstream
        logger.warning("Could not patch Anthropic max-effort ceiling (%s).", exc)
        return

    original = AnthropicAPI.max_tokens_for_config

    def _max_tokens_at_model_ceiling(self, config):
        result = original(self, config)
        if result is None:
            return result
        effort = self.effort_from_reasoning_effort(config)
        if effort in ("xhigh", "max") and (
            self.is_claude_frontier() or self.is_claude_3_7()
        ):
            return max(result, 128_000)
        return result

    AnthropicAPI.max_tokens_for_config = _max_tokens_at_model_ceiling


# When a request asks for more output tokens than the model allows, Bedrock's
# validation error states the real ceiling ("...exceeds the model limit of
# 128000..."). That makes the API itself the source of truth for every
# model's max output — no hand-maintained per-model table.
_MODEL_LIMIT_RE = re.compile(r"exceeds the model limit of (\d+)")

# Deliberately above every current model's ceiling. First call per model gets
# an instant, unbilled validation error carrying the true limit; we cache it
# and retry. Models whose ceiling ever exceeds this simply run capped at it.
_PROBE_MAX_TOKENS = 512_000

# model_name -> discovered max output tokens, per subprocess.
_discovered_max_tokens: "dict[str, int]" = {}


def _limit_from_result(result: "object") -> "int | None":
    """Extract the model's output-token limit from a generate() result, if the
    result is Bedrock's oversized-max_tokens validation error."""
    ex = result[0] if isinstance(result, tuple) else None
    if not isinstance(ex, Exception):
        return None
    match = _MODEL_LIMIT_RE.search(str(ex))
    return int(match.group(1)) if match else None


def _patch_bedrock_default_max_tokens() -> None:
    """Always run at the model's true output ceiling, discovered from Bedrock.

    Inspect's default (constant 2048) and Bedrock's omitted-maxTokens default
    (4096 on claude-sonnet-5, measured live — despite AWS docs claiming "the
    maximum allowed value") both silently truncate long completions and
    thinking mid-stream. Users should never hand-tune max_tokens in an eval.

    So: when the caller sets no max_tokens, request ``_PROBE_MAX_TOKENS``.
    If any request exceeds the model's real ceiling, Bedrock's validation
    error names that ceiling — cache it and retry at exactly the model max.
    One instant, unbilled failed call per model per subprocess; correct for
    every current and future model with zero hardcoded knowledge.

    Claude models normally bypass this entirely — they route through
    Inspect's native anthropic provider (eval_mcp/core/model_routing.py),
    which sizes max_tokens itself. This patch covers the families still on
    Converse (Nova, Llama, Mistral, gpt-oss, ...; Nova's real ceiling is
    10,000, measured live — the same truncation exposure).
    """
    try:
        from inspect_ai.model._providers.bedrock import BedrockAPI
    except Exception as exc:  # pragma: no cover - import shape changed upstream
        logger.warning(
            "Could not patch Inspect Bedrock max_tokens default (%s); reasoning "
            "models may return empty completions truncated at 2048.",
            exc,
        )
        return

    def _default_max_tokens(self) -> "int | None":
        return _discovered_max_tokens.get(
            getattr(self, "model_name", ""), _PROBE_MAX_TOKENS
        )

    BedrockAPI.max_tokens = _default_max_tokens

    original_generate = BedrockAPI.generate

    async def _generate_discovering_limit(self, input, tools, tool_choice, config):
        while True:
            result = await original_generate(self, input, tools, tool_choice, config)
            limit = _limit_from_result(result)
            if limit is None:
                return result
            if config.max_tokens is not None and config.max_tokens <= limit:
                return result  # not an over-ask we can fix — surface it
            _discovered_max_tokens[self.model_name] = limit
            config = config.model_copy(update={"max_tokens": limit})

    BedrockAPI.generate = _generate_discovering_limit


def _patch_bedrock_gpt5_reasoning_effort() -> None:
    """Make ``reasoning_effort`` reach GPT-5.x models on the Converse path.

    Inspect's ``reasoning_config()`` only builds reasoning fields for Claude,
    gpt-oss, and Nova — for GPT-5.x (e.g. ``global.openai.gpt-5.6-sol``) the
    setting is silently dropped. The models themselves accept the
    Responses-API shape through Converse's ``additionalModelRequestFields``:
    ``{"reasoning": {"effort": ...}}``. Verified live 2026-08-19 — an invalid
    value is rejected by the model with "Supported values are: 'none', 'low',
    'medium', 'high', 'xhigh', and 'max'". Remove when upstream adds the
    branch.
    """
    try:
        from inspect_ai.model._providers.bedrock import BedrockAPI
    except Exception as exc:  # pragma: no cover - import shape changed upstream
        logger.warning(
            "Could not patch Inspect Bedrock GPT-5 reasoning effort (%s); "
            "reasoning_effort will be silently ignored on GPT-5.x Converse "
            "models.",
            exc,
        )
        return

    original = BedrockAPI.reasoning_config

    def _reasoning_config_with_gpt5(self, config):
        fields = original(self, config)
        if (
            not fields
            and config.reasoning_effort is not None
            and "gpt-5" in self.model_family().lower()
        ):
            fields = {"reasoning": {"effort": config.reasoning_effort}}
        return fields

    BedrockAPI.reasoning_config = _reasoning_config_with_gpt5


def _patch_bedrock_redacted_reasoning() -> None:
    """Tolerate redacted (encrypted) reasoning blocks on the Converse path.

    GPT-5.x at xhigh/max effort returns ``reasoningContent.redactedContent``
    instead of ``reasoningText``; upstream's ``ConverseReasoningContent``
    schema requires ``reasoningText``, so every such response crashes with a
    pydantic ValidationError (verified live on gpt-5.6-sol, 2026-08-19).

    Fix: make ``reasoningText`` optional and accept ``redactedContent`` in
    the schema, then map redacted-only blocks to an empty reasoning text
    before conversion — the content is encrypted and unreadable by design;
    what matters is that the run doesn't die and usage still counts. Remove
    when upstream models redacted reasoning.
    """
    try:
        from typing import Optional

        from pydantic.fields import FieldInfo

        from inspect_ai.model._providers import bedrock as _bedrock
    except Exception as exc:  # pragma: no cover - import shape changed upstream
        logger.warning(
            "Could not patch Inspect Bedrock redacted reasoning (%s); GPT-5.x "
            "responses at xhigh/max effort will fail to parse.",
            exc,
        )
        return

    rc = _bedrock.ConverseReasoningContent
    rc.__pydantic_fields__["reasoningText"] = FieldInfo(
        annotation=Optional[_bedrock.ConverseReasoningText], default=None
    )
    rc.__pydantic_fields__["redactedContent"] = FieldInfo(
        annotation=Optional[bytes], default=None
    )
    rc.model_rebuild(force=True)
    # Parents cache the child's core schema — rebuild the chain.
    for name in ("ConverseMessageContent", "ConverseMessage", "ConverseOutput", "ConverseResponse"):
        cls = getattr(_bedrock, name, None)
        if cls is not None:
            cls.model_rebuild(force=True)

    original = _bedrock.model_output_from_response

    def _model_output_tolerating_redacted(model, response, tools):
        for c in response.output.message.content:
            if c.reasoningContent is not None and c.reasoningContent.reasoningText is None:
                c.reasoningContent.reasoningText = _bedrock.ConverseReasoningText(text="")
        return original(model, response, tools)

    _bedrock.model_output_from_response = _model_output_tolerating_redacted


# Apply on import so a generated task file gets the patch simply by importing
# this module — no call site to forget.
apply_inspect_patches()
