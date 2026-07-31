"""Solver that generates at each model's own output-token limit.

Why this exists: Inspect AI's Bedrock provider supplies a *constant* default
``max_tokens`` when the caller passes none —
``inspect_ai/_util/constants.py:DEFAULT_MAX_TOKENS = 2048``, routed through a
hand-coded family table in ``_providers/bedrock.py``. For reasoning models that
default is actively wrong, not merely small: they can spend the whole budget on
the reasoning channel and return an EMPTY completion with
``stop_reason="max_tokens"``. Reproduced on stock Inspect with nothing set —
gpt-oss-20b on Converse returned 2048 output tokens and zero visible characters.
An empty answer scores 0 while the run reports success, so the model that
reasoned hardest looks like the worst one.

Passing a single ``--max-tokens`` doesn't fix it, because that flag is global
while the real limits are per model and span a wide range (verified against live
Bedrock: llama3.3-70b 4,096; Nova Pro 10,000; Claude Haiku 4.5 64,000;
gpt-oss-20b 128,000). Exceeding a model's limit is a hard ValidationException —
"The maximum tokens you requested exceeds the model limit of 10000" — not a
clamp, so one value safe for the largest model kills the smallest, and a value
safe for all of them is ~4k, which truncates the models we most want to measure.

So resolve it per model at solve time, where the active model is known. That
keeps every model in ONE subprocess (Inspect already runs targets concurrently,
so splitting per model would serialise what is currently parallel) while giving
each its own ceiling.

Mantle (``openai/bedrock/*``) is deliberately left unbounded: Inspect's OpenAI
provider has no ``max_tokens()`` override, so omitting the value lets the model
run to its own limit — measured, gpt-5.6-sol produced 8,413 output tokens on a
prompt that a hardcoded 8,192 truncated.

This is a workaround for an upstream gap. When Inspect's Bedrock provider
resolves real per-model limits instead of a family constant, delete this module
and go back to a plain ``generate()``.
"""

from __future__ import annotations

import logging

from inspect_ai.model import GenerateConfig, get_model
from inspect_ai.solver import solver

logger = logging.getLogger(__name__)

# Used only when the model is absent from the limits dataset. Above Inspect's
# 2048 (which demonstrably produces empty completions) but low enough to be
# accepted by every Bedrock model we've measured, since an unknown model's real
# ceiling can't be checked. Unknown means "can't reason about it", not
# "unlimited" — omitting the value entirely would hand it Inspect's 2048.
FALLBACK_MAX_TOKENS = 8192


def resolve_max_tokens(model_id: str) -> int | None:
    """Ceiling for ``model_id``, or None to leave generation unbounded.

    None for Mantle models — see the module docstring. Otherwise the model's
    advertised maximum, falling back to ``FALLBACK_MAX_TOKENS`` when unknown.
    """
    if model_id.startswith("openai/bedrock/"):
        return None

    try:
        from eval_mcp.core.pricing import get_max_output_tokens

        limit = get_max_output_tokens(model_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("max-output-token lookup failed for %s: %s", model_id, exc)
        limit = None

    return limit or FALLBACK_MAX_TOKENS


@solver
def generate_at_model_limit(**kwargs):
    """``generate()``, but with ``max_tokens`` resolved from the active model.

    ``kwargs`` are forwarded to the underlying generate call, so this is a
    drop-in replacement for ``generate()`` in a solver chain. An explicit
    ``max_tokens`` in ``kwargs`` wins — a caller who asked for a specific
    ceiling gets it.
    """
    async def solve(state, generate):
        if "max_tokens" in kwargs:
            return await generate(state, **kwargs)

        # `get_model()` with no argument returns the model this task is
        # currently running against, which is what makes a single subprocess
        # able to serve several models at their own limits.
        try:
            model_id = str(get_model())
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("could not resolve active model: %s", exc)
            return await generate(state, **kwargs)

        max_tokens = resolve_max_tokens(model_id)
        if max_tokens is None:
            return await generate(state, **kwargs)

        return await generate(state, max_tokens=max_tokens, **kwargs)

    return solve


__all__ = ["generate_at_model_limit", "resolve_max_tokens", "FALLBACK_MAX_TOKENS"]
