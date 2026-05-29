"""Pure-logic tests for the RAG scorer aggregation helpers.

Bedrock and the Inspect AI runtime are intentionally NOT mocked — only
the deterministic pieces (score formulas, chunk formatting, verdict
parsing) are tested here. End-to-end behaviour is validated by running
the MCP from Claude Code with a real judge (see step 5 in the plan).
"""

import pytest

from eval_mcp.scorers.rag import (
    RAG_PROMPT_TEMPLATE,
    _format_chunks,
    _fraction,
    _not_no_fraction,
    _summarise,
    _weighted_precision_at_k,
    _yes_fraction,
    configure_judge,
)


def test_format_chunks_uses_1_based_index() -> None:
    out = _format_chunks(["alpha", "beta"])
    assert out == "[chunk 1] alpha\n[chunk 2] beta"


def test_format_chunks_empty() -> None:
    assert _format_chunks([]) == ""


def test_fraction_handles_zero_denominator() -> None:
    assert _fraction(0, 0) == 0.0
    assert _fraction(3, 0) == 0.0
    assert _fraction(2, 4) == 0.5


# --- _not_no_fraction: DeepEval faithfulness / answer_relevancy ---
# yes AND idk count as good; only "no" fails. Empty list → 1.0 (DeepEval
# returns 1 when there are no claims/statements).

def test_not_no_counts_yes_and_idk() -> None:
    verdicts = [
        {"verdict": "yes"},
        {"verdict": "idk"},
        {"verdict": "no"},
        {"verdict": "no"},
    ]
    assert _not_no_fraction(verdicts) == 0.5


def test_not_no_all_yes_is_one() -> None:
    assert _not_no_fraction([{"verdict": "yes"}, {"verdict": "idk"}]) == 1.0


def test_not_no_empty_is_one() -> None:
    # Matches DeepEval: no claims → faithfulness 1.0
    assert _not_no_fraction([]) == 1.0


def test_not_no_case_insensitive() -> None:
    assert _not_no_fraction([{"verdict": "NO"}, {"verdict": "Yes"}]) == 0.5


# --- _yes_fraction: DeepEval contextual_recall / contextual_relevancy ---
# only explicit "yes" counts; empty → 0.0.

def test_yes_fraction() -> None:
    verdicts = [
        {"verdict": "yes"},
        {"verdict": "no"},
        {"verdict": "yes"},
        {"verdict": "yes"},
    ]
    assert _yes_fraction(verdicts) == 0.75


def test_yes_fraction_empty_is_zero() -> None:
    assert _yes_fraction([]) == 0.0


def test_yes_fraction_idk_does_not_count() -> None:
    # Unlike _not_no_fraction, idk is NOT counted here.
    assert _yes_fraction([{"verdict": "yes"}, {"verdict": "idk"}]) == 0.5


# --- _weighted_precision_at_k: DeepEval contextual_precision ---
# Operates on verdicts in the order given (retriever rank order).

def test_precision_at_k_all_relevant() -> None:
    verdicts = [{"verdict": "yes"}, {"verdict": "yes"}, {"verdict": "yes"}]
    assert _weighted_precision_at_k(verdicts) == 1.0


def test_precision_at_k_no_relevant() -> None:
    assert _weighted_precision_at_k([{"verdict": "no"}, {"verdict": "no"}]) == 0.0


def test_precision_at_k_empty() -> None:
    assert _weighted_precision_at_k([]) == 0.0


def test_precision_at_k_irrelevant_first_punishes() -> None:
    # no, yes → relevant at k=2: precision@2 = 1/2, over 1 relevant.
    verdicts = [{"verdict": "no"}, {"verdict": "yes"}]
    assert _weighted_precision_at_k(verdicts) == pytest.approx(0.5)


def test_precision_at_k_relevant_first_rewards() -> None:
    # yes, no → relevant at k=1: precision@1 = 1/1, over 1 relevant.
    verdicts = [{"verdict": "yes"}, {"verdict": "no"}]
    assert _weighted_precision_at_k(verdicts) == 1.0


def test_precision_at_k_mixed_order() -> None:
    # no, yes, yes (in list order). relevant at k=2 (1/2) and k=3 (2/3),
    # over 2 relevant → (1/2 + 2/3) / 2.
    verdicts = [{"verdict": "no"}, {"verdict": "yes"}, {"verdict": "yes"}]
    expected = (0.5 + (2 / 3)) / 2
    assert _weighted_precision_at_k(verdicts) == pytest.approx(expected)


def test_summarise_truncates() -> None:
    verdicts = [{"verdict": "yes", "statement": "s", "reason": "r"} for _ in range(20)]
    out = _summarise(verdicts, limit=3)
    assert "(17 more)" in out
    assert out.count("[yes]") == 3


def test_summarise_empty_message() -> None:
    assert "no verdicts" in _summarise([])


def test_rag_prompt_template_has_placeholders() -> None:
    # Sanity: changing this template should be intentional — solver
    # depends on these exact slots.
    rendered = RAG_PROMPT_TEMPLATE.format(context="C", question="Q")
    assert "CONTEXT:" in rendered
    assert "QUESTION: Q" in rendered
    assert "ONLY the provided context" in rendered


def test_configure_judge_is_module_global() -> None:
    # Cycle the global to confirm configure_judge mutates it (and
    # reset to a known value so it doesn't leak into other tests).
    from eval_mcp.scorers import rag as rag_mod

    configure_judge("mockllm/model-A")
    assert rag_mod._DEFAULT_JUDGE_MODEL == "mockllm/model-A"
    configure_judge("mockllm/model-B")
    assert rag_mod._DEFAULT_JUDGE_MODEL == "mockllm/model-B"


# ---------------------------------------------------------------------------
# rag_prompt_solver behaviour — covers default wrap + {context} placeholder
# ---------------------------------------------------------------------------


class _StubMsg:
    """Minimal stand-in for ChatMessageUser so we can drive solve() in tests."""

    def __init__(self, content: str) -> None:
        self.role = "user"
        self.text = content
        self.content = content


class _StubState:
    def __init__(self, message_text: str, metadata: dict) -> None:
        self.metadata = metadata
        self.messages = [_StubMsg(message_text)]


async def _noop_generate(state):  # pragma: no cover - never called by solver
    return state


def _run_solver(state):
    """Sync helper to drive the async rag_prompt_solver in-process."""
    import asyncio

    from eval_mcp.scorers.rag import rag_prompt_solver

    solver = rag_prompt_solver()
    return asyncio.run(solver(state, _noop_generate))


def test_rag_solver_wraps_when_no_context_placeholder() -> None:
    state = _StubState(
        "What is the capital of France?",
        {"retrieval_context": ["Paris is the capital of France."]},
    )
    out = _run_solver(state)
    content = out.messages[0].content
    # Default wrap: our template framed around the user's bare question
    assert "Answer the following question using ONLY" in content
    assert "[chunk 1] Paris is the capital of France." in content
    assert "QUESTION: What is the capital of France?" in content


def test_rag_solver_substitutes_context_placeholder() -> None:
    # User-tuned prompt with {context} placeholder — solver should NOT
    # apply the default wrap, only substitute the chunks in place.
    template = (
        "You are a careful assistant. Use ONLY these passages:\n"
        "{context}\n"
        "Think step by step, then answer: What is the capital of France?"
    )
    state = _StubState(template, {"retrieval_context": ["Paris is the capital."]})
    out = _run_solver(state)
    content = out.messages[0].content
    # The wrap header is NOT present
    assert "Answer the following question using ONLY" not in content
    # Chunks landed where the placeholder was
    assert "[chunk 1] Paris is the capital." in content
    # User-controlled instructions are preserved verbatim
    assert "You are a careful assistant" in content
    assert "Think step by step" in content
    # No leftover placeholder
    assert "{context}" not in content


def test_rag_solver_noop_without_retrieval_context() -> None:
    state = _StubState("Bare question.", {})
    out = _run_solver(state)
    # State pass-through, message unchanged
    assert out.messages[0].content == "Bare question."


def test_rag_solver_handles_multiple_context_placeholders() -> None:
    # Edge case: template references {context} twice (e.g. for emphasis).
    # Both occurrences should get substituted, not just the first.
    template = "Context: {context}\n---\nReference again: {context}\nAnswer:"
    state = _StubState(template, {"retrieval_context": ["chunk-A"]})
    out = _run_solver(state)
    content = out.messages[0].content
    assert content.count("[chunk 1] chunk-A") == 2
    assert "{context}" not in content
