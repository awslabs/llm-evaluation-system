"""Default jury composition and the Nemotron max_tokens failure.

Two separate things pinned here, both from 2026-09-23:

1. The default jury: one judge per family, no Nemotron.
2. The bug that made Nemotron fail every call. Our Bedrock patch probes
   max_tokens high and retries at the limit Bedrock names. For Nemotron that
   "limit" is the whole context window, shared with the prompt, so the retry
   asked for window-sized output and left no room for input:

       maximum context length is 262144 tokens. However, you requested
       262144 output tokens and your prompt contains 99 characters ...

   These tests drive the retry loop with a fake Bedrock that answers exactly
   the way the live endpoint did.
"""
from __future__ import annotations

import asyncio

import pytest
from inspect_ai.model import ChatMessageUser, GenerateConfig

import eval_mcp.inspect_patches as patches
from eval_mcp.core.judge_config import JUDGE_MODELS

WINDOW = 262144
LIMIT_ERR = (
    "ValidationException: The maximum tokens you requested exceeds the model "
    f"limit of {WINDOW}. Try again with a maximum tokens value that is lower "
    f"than {WINDOW}."
)


def _context_err(requested: int, chars: int) -> str:
    return (
        f"ValidationException: maximum context length is {WINDOW} tokens. "
        f"However, you requested {requested} output tokens and your prompt "
        f"contains {chars} characters (more than 0 characters, which is the "
        "upper bound for 0 input tokens)."
    )


class _Api:
    model_name = "nvidia.nemotron-fake"


class _FakeNemotron:
    """Accepts max_tokens only if output + prompt fits the shared window.
    Returns results in Inspect's (exception, call) shape on failure."""

    def __init__(self, prompt_chars: int = 1778):
        self.prompt_chars = prompt_chars
        self.asked: list[int | None] = []

    async def __call__(self, api, input, tools, tool_choice, config):
        mt = config.max_tokens if config.max_tokens is not None else patches._PROBE_MAX_TOKENS
        self.asked.append(config.max_tokens)
        if mt > WINDOW:
            return (RuntimeError(LIMIT_ERR), None)
        if mt + self.prompt_chars > WINDOW:
            return (RuntimeError(_context_err(mt, self.prompt_chars)), None)
        return "OK"


@pytest.fixture(autouse=True)
def _clean_caches():
    for cache in (patches._discovered_max_tokens, patches._context_windows):
        cache.pop(_Api.model_name, None)
    yield
    for cache in (patches._discovered_max_tokens, patches._context_windows):
        cache.pop(_Api.model_name, None)


def _run(fake, config=None, text="Q: 2+2? Ref: 4. Answer: 4."):
    return asyncio.run(
        patches._generate_within_limits(
            fake, _Api(), [ChatMessageUser(content=text)], [], None,
            config or GenerateConfig(),
        )
    )


def test_default_jury_is_one_judge_per_family_without_nemotron():
    assert JUDGE_MODELS == {
        "claude": "bedrock/us.anthropic.claude-sonnet-5",
        "nova": "bedrock/us.amazon.nova-pro-v1:0",
        "gpt": "bedrock/us.openai.gpt-6-luna",
    }
    assert not any("nemotron" in m for m in JUDGE_MODELS.values())


def test_context_window_model_recovers_instead_of_failing():
    fake = _FakeNemotron()
    assert _run(fake) == "OK"
    # probe -> output-limit error -> window-sized retry -> context error ->
    # retry with the room actually left after the prompt.
    assert fake.asked[:2] == [None, WINDOW]
    final = fake.asked[-1]
    assert final is not None and final + fake.prompt_chars <= WINDOW


def test_budget_is_learned_once_per_model_not_per_call():
    _run(_FakeNemotron())
    second = _FakeNemotron()
    assert _run(second) == "OK"
    assert len(second.asked) == 1, (
        f"every call re-paid the failed round-trips: {second.asked}"
    )


def test_a_small_explicit_max_tokens_is_never_raised():
    _run(_FakeNemotron())  # learn the window
    fake = _FakeNemotron()
    assert _run(fake, GenerateConfig(max_tokens=500)) == "OK"
    assert fake.asked == [500]


def test_a_prompt_that_fills_the_window_is_surfaced_not_looped():
    fake = _FakeNemotron(prompt_chars=WINDOW)
    result = _run(fake)
    assert isinstance(result, tuple) and "maximum context length" in str(result[0])
    assert len(fake.asked) <= 3


def test_ordinary_output_cap_models_are_unchanged():
    """A model whose limit is a true output cap (most of them) still gets the
    original single discovery retry and nothing else."""

    class _CapOnly:
        asked: list = []

        async def __call__(self, api, input, tools, tool_choice, config):
            mt = config.max_tokens if config.max_tokens is not None else patches._PROBE_MAX_TOKENS
            self.asked.append(config.max_tokens)
            return (RuntimeError(LIMIT_ERR), None) if mt > WINDOW else "OK"

    fake = _CapOnly()
    assert _run(fake) == "OK"
    assert fake.asked == [None, WINDOW]
    assert _Api.model_name not in patches._context_windows


def test_unrelated_errors_pass_through_untouched():
    async def throttled(api, input, tools, tool_choice, config):
        return (RuntimeError("ThrottlingException: slow down"), None)

    result = _run(throttled)
    assert "ThrottlingException" in str(result[0])
