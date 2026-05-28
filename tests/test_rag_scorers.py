"""Pure-logic tests for the RAG scorer aggregation helpers.

Bedrock and the Inspect AI runtime are intentionally NOT mocked — only
the deterministic pieces (score formulas, chunk formatting, verdict
parsing) are tested here. End-to-end behaviour is validated by running
the MCP from Claude Code with a real judge (see step 5 in the plan).
"""

import pytest

from eval_mcp.scorers.rag import (
    RAG_PROMPT_TEMPLATE,
    _binary_yes_fraction,
    _faithfulness_score,
    _format_chunks,
    _fraction,
    _precision_at_k,
    _summarise_verdicts,
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


def test_faithfulness_counts_yes_and_idk_as_faithful() -> None:
    verdicts = [
        {"verdict": "yes"},
        {"verdict": "idk"},
        {"verdict": "no"},
        {"verdict": "no"},
    ]
    assert _faithfulness_score(verdicts) == 0.5


def test_faithfulness_all_yes_is_one() -> None:
    assert _faithfulness_score([{"verdict": "yes"}, {"verdict": "yes"}]) == 1.0


def test_faithfulness_empty_is_zero() -> None:
    assert _faithfulness_score([]) == 0.0


def test_binary_yes_fraction() -> None:
    verdicts = [
        {"verdict": "yes"},
        {"verdict": "no"},
        {"verdict": "yes"},
        {"verdict": "yes"},
    ]
    assert _binary_yes_fraction(verdicts) == 0.75


def test_precision_at_k_all_relevant() -> None:
    # 3 chunks, all relevant. precision@1=1, @2=1, @3=1. Mean=1.
    verdicts = [
        {"chunk_index": 1, "verdict": "yes"},
        {"chunk_index": 2, "verdict": "yes"},
        {"chunk_index": 3, "verdict": "yes"},
    ]
    assert _precision_at_k(verdicts) == 1.0


def test_precision_at_k_no_relevant() -> None:
    verdicts = [
        {"chunk_index": 1, "verdict": "no"},
        {"chunk_index": 2, "verdict": "no"},
    ]
    assert _precision_at_k(verdicts) == 0.0


def test_precision_at_k_irrelevant_first_punishes() -> None:
    # Irrelevant first, then relevant: precision@2 = 1/2 over 1 relevant.
    verdicts = [
        {"chunk_index": 1, "verdict": "no"},
        {"chunk_index": 2, "verdict": "yes"},
    ]
    assert _precision_at_k(verdicts) == pytest.approx(0.5)


def test_precision_at_k_relevant_first_rewards() -> None:
    # Relevant first, then irrelevant: precision@1 = 1/1 over 1 relevant.
    verdicts = [
        {"chunk_index": 1, "verdict": "yes"},
        {"chunk_index": 2, "verdict": "no"},
    ]
    assert _precision_at_k(verdicts) == 1.0


def test_precision_at_k_respects_chunk_index_order() -> None:
    # Verdicts returned out of order — function must sort by chunk_index
    # before computing precision-at-k. (Judges return arrays in arbitrary
    # order sometimes.)
    verdicts = [
        {"chunk_index": 3, "verdict": "yes"},
        {"chunk_index": 1, "verdict": "no"},
        {"chunk_index": 2, "verdict": "yes"},
    ]
    # Sorted: no, yes, yes. Precision@2 + precision@3 / 2 = (1/2 + 2/3)/2.
    expected = (0.5 + (2 / 3)) / 2
    assert _precision_at_k(verdicts) == pytest.approx(expected)


def test_summarise_verdicts_truncates() -> None:
    verdicts = [{"verdict": "yes", "claim": "c", "reason": "r"} for _ in range(20)]
    out = _summarise_verdicts(verdicts, limit=3)
    assert "(17 more)" in out
    # Three actual lines + the ellipsis line
    assert out.count("[yes]") == 3


def test_summarise_verdicts_empty_message() -> None:
    assert "no verdicts" in _summarise_verdicts([])


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
