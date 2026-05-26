"""DeepEval-style RAG scorers for Inspect AI.

Each scorer reads ``state.metadata["retrieval_context"]`` (a
``list[str]`` of retrieved chunks in the retriever's ranking order)
plus the usual ``(state.input, state.output.completion, target.text)``
trio, runs ONE LLM-judge call with a forced-tool-output schema, and
returns a 0..1 score with an explanation string.

The six metrics mirror DeepEval's RAG suite:

- ``faithfulness``       — fraction of answer claims that agree with chunks.
- ``answer_relevancy``   — fraction of answer statements that address the question.
- ``contextual_precision``— precision-at-k of relevant chunks (rewards relevant chunks ranked early).
- ``contextual_recall``  — fraction of expected-answer sentences backed by some chunk.
- ``contextual_relevancy``— fraction of chunk-level statements relevant to the question.
- ``hallucination``      — 1 minus the contradiction rate (higher = more grounded).

Why one call per metric (vs DeepEval's two-step extract-then-verdict)?
Modern Bedrock judges have a long-enough output budget to do extraction
+ verdict in one tool call, halving cost. The shape DeepEval uses
(claim list, then verdict list) is preserved inside the tool-call
arguments — we just don't split it across two HTTP turns.

Judge selection: each scorer factory accepts ``judge_model``. The
generated task file passes the first model from ``CONFIG["judge_models"]``
via :func:`configure_judge`, so users get the same judge they configured
for the jury. Override per-scorer with ``faithfulness(judge_model=...)``.
"""

from __future__ import annotations

from typing import List, Optional

from inspect_ai.model import ChatMessageSystem, ChatMessageUser, get_model
from inspect_ai.scorer import Score, mean, scorer, stderr
from inspect_ai.solver import solver
from inspect_ai.tool._tool_info import ToolInfo
from inspect_ai.tool._tool_params import ToolParams


_DEFAULT_JUDGE_MODEL: Optional[str] = None


def configure_judge(model_id: str) -> None:
    """Set the default judge model for RAG scorers in this process.

    The generated task file calls this once at module import so each
    scorer factory below can run with no explicit ``judge_model`` arg.
    Tests / callers that want per-call control can pass ``judge_model=``
    directly to any scorer factory.
    """
    global _DEFAULT_JUDGE_MODEL
    _DEFAULT_JUDGE_MODEL = model_id


def _resolve_judge(judge_model: Optional[str]) -> str:
    j = judge_model or _DEFAULT_JUDGE_MODEL
    if not j:
        raise ValueError(
            "RAG scorer needs a judge model. Either pass judge_model= to the "
            "scorer or call eval_mcp.scorers.rag.configure_judge(model_id) at "
            "task-file import time."
        )
    return j


def _require_retrieval_context(state, scorer_name: str) -> List[str]:
    rc = (state.metadata or {}).get("retrieval_context")
    if not rc:
        raise ValueError(
            f"Sample is missing retrieval_context — required by the "
            f"{scorer_name} scorer. Add a 'retrieval_context' column "
            f"(list[str]) when saving the dataset."
        )
    if not isinstance(rc, list) or any(not isinstance(c, str) for c in rc):
        raise ValueError(
            f"retrieval_context must be a list[str]; got {type(rc).__name__} "
            f"for sample with {len(rc) if hasattr(rc, '__len__') else '?'} entries."
        )
    return rc


async def _judge_with_tool(
    judge_model: str,
    system_prompt: str,
    user_prompt: str,
    tool: ToolInfo,
) -> dict:
    """Run one tool-forced judge call. Returns the tool's arguments dict.

    Raises ``RuntimeError`` if the judge didn't call the tool or returned
    no parseable arguments — the scorer surfaces that as a 0.0 with an
    explanation rather than crashing the whole eval.
    """
    judge = get_model(judge_model)
    result = await judge.generate(
        [
            ChatMessageSystem(content=system_prompt),
            ChatMessageUser(content=user_prompt),
        ],
        tools=[tool],
        tool_choice="any",
    )

    if not result or not result.message or not result.message.tool_calls:
        body = result.completion[:200] if result and result.completion else "(empty)"
        raise RuntimeError(f"Judge did not call the tool. Response: {body}")

    args: dict = {}
    for tc in result.message.tool_calls:
        if tc.function == tool.name:
            args.update(tc.arguments)
    if not args:
        raise RuntimeError(f"Judge called no tool named {tool.name!r}.")
    return args


def _format_chunks(chunks: List[str]) -> str:
    return "\n".join(f"[chunk {i + 1}] {c}" for i, c in enumerate(chunks))


# ---------------------------------------------------------------------------
# RAG-aware solver: injects retrieval_context into the candidate model's
# prompt before the model generates. Slots into the solver chain BEFORE
# generate(), so the standard generate() solver still issues the model call.
# ---------------------------------------------------------------------------


RAG_PROMPT_TEMPLATE = (
    "Answer the following question using ONLY the provided context. "
    "If the context does not contain the answer, say so explicitly.\n\n"
    "CONTEXT:\n{context}\n\n"
    "QUESTION: {question}"
)


@solver
def rag_prompt_solver():
    """Wrap the last user message with the retrieved chunks before generation.

    No-op when ``state.metadata['retrieval_context']`` is absent or empty
    (so the same task file works for samples without RAG context, e.g. a
    smoke test). When present, the chunks are formatted in retriever
    rank order and prepended as ``CONTEXT:`` above the original question.
    """
    async def solve(state, generate):
        chunks = (state.metadata or {}).get("retrieval_context")
        if not chunks:
            return state
        formatted = _format_chunks(list(chunks))
        for i in range(len(state.messages) - 1, -1, -1):
            msg = state.messages[i]
            if getattr(msg, "role", None) == "user":
                existing = msg.text if hasattr(msg, "text") else str(getattr(msg, "content", ""))
                state.messages[i] = ChatMessageUser(
                    content=RAG_PROMPT_TEMPLATE.format(
                        context=formatted, question=existing
                    )
                )
                break
        return state

    return solve


# ---------------------------------------------------------------------------
# Tool schemas — one per scorer, each named uniquely so the judge can't
# confuse them across compositions (e.g. when faithfulness + hallucination
# both run on the same sample, the tool calls don't collide).
# ---------------------------------------------------------------------------


def _verdict_list_tool(name: str, description: str, verdict_values: List[str]) -> ToolInfo:
    """Build a tool that takes a list of {claim, verdict, reason} verdicts."""
    return ToolInfo(
        name=name,
        description=description,
        parameters=ToolParams(
            type="object",
            properties={
                "verdicts": {
                    "type": "array",
                    "description": (
                        "One entry per atomic claim/statement extracted from "
                        "the source text. Order does not matter."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "claim": {
                                "type": "string",
                                "description": "The atomic claim being judged.",
                            },
                            "verdict": {
                                "type": "string",
                                "enum": verdict_values,
                                "description": (
                                    "Verdict relative to the reference. "
                                    f"Allowed: {verdict_values}."
                                ),
                            },
                            "reason": {
                                "type": "string",
                                "description": "Short justification for the verdict.",
                            },
                        },
                        "required": ["claim", "verdict"],
                    },
                },
            },
            required=["verdicts"],
        ),
    )


def _chunk_verdict_tool(name: str, description: str) -> ToolInfo:
    """Build a tool that emits ONE verdict per chunk in order."""
    return ToolInfo(
        name=name,
        description=description,
        parameters=ToolParams(
            type="object",
            properties={
                "chunk_verdicts": {
                    "type": "array",
                    "description": (
                        "MUST have exactly one entry per retrieved chunk, in "
                        "the same order as the chunks were given."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "chunk_index": {
                                "type": "integer",
                                "description": "1-based index of the chunk.",
                            },
                            "verdict": {
                                "type": "string",
                                "enum": ["yes", "no"],
                                "description": "yes if the chunk is relevant; no otherwise.",
                            },
                            "reason": {
                                "type": "string",
                                "description": "Short justification.",
                            },
                        },
                        "required": ["chunk_index", "verdict"],
                    },
                },
            },
            required=["chunk_verdicts"],
        ),
    )


# ---------------------------------------------------------------------------
# Aggregation helpers — pure functions, tested in tests/test_rag_scorers.py.
# ---------------------------------------------------------------------------


def _fraction(numer: int, denom: int) -> float:
    return numer / denom if denom > 0 else 0.0


def _faithfulness_score(verdicts: List[dict]) -> float:
    """yes + idk counted as faithful; no penalises. Matches DeepEval default."""
    if not verdicts:
        return 0.0
    faithful = sum(1 for v in verdicts if v.get("verdict") in ("yes", "idk"))
    return _fraction(faithful, len(verdicts))


def _binary_yes_fraction(verdicts: List[dict]) -> float:
    if not verdicts:
        return 0.0
    yes = sum(1 for v in verdicts if v.get("verdict") == "yes")
    return _fraction(yes, len(verdicts))


def _precision_at_k(chunk_verdicts: List[dict]) -> float:
    """DeepEval-style precision-at-k weighted by ranking position.

    For each relevant chunk at rank k (1-based), add precision@k. Divide
    by the total count of relevant chunks. 0 if no chunk is relevant.
    """
    if not chunk_verdicts:
        return 0.0
    ordered = sorted(
        chunk_verdicts,
        key=lambda v: int(v.get("chunk_index", 0)),
    )
    total_relevant = sum(1 for v in ordered if v.get("verdict") == "yes")
    if total_relevant == 0:
        return 0.0
    relevant_so_far = 0
    accum = 0.0
    for k, v in enumerate(ordered, start=1):
        if v.get("verdict") == "yes":
            relevant_so_far += 1
            accum += relevant_so_far / k
    return accum / total_relevant


def _summarise_verdicts(verdicts: List[dict], limit: int = 6) -> str:
    if not verdicts:
        return "(no verdicts returned by judge)"
    lines = []
    for v in verdicts[:limit]:
        verdict = v.get("verdict", "?")
        claim = (v.get("claim") or "")[:80]
        reason = (v.get("reason") or "")[:80]
        lines.append(f"  [{verdict}] {claim} — {reason}")
    if len(verdicts) > limit:
        lines.append(f"  ... ({len(verdicts) - limit} more)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Scorers
# ---------------------------------------------------------------------------


@scorer(metrics=[mean(), stderr()])
def faithfulness(judge_model: Optional[str] = None):
    """Fraction of claims in the answer that agree with the retrieved chunks.

    yes/idk counted as faithful (idk = "the chunk neither confirms nor
    contradicts"). DeepEval default behaviour.
    """
    async def score(state, target):
        chunks = _require_retrieval_context(state, "faithfulness")
        judge = _resolve_judge(judge_model)
        answer = state.output.completion if state.output else ""
        if not answer.strip():
            return Score(value=0.0, explanation="No answer produced.")

        system = (
            "You are a grounding judge. Given a model's answer and a list of "
            "retrieved context chunks, extract every atomic factual claim from "
            "the answer and assign each claim a verdict relative to the chunks:\n"
            "  yes  — the chunks support the claim\n"
            "  no   — the chunks contradict the claim or it is fabricated\n"
            "  idk  — the chunks neither confirm nor contradict\n"
            "Return the list via the submit_faithfulness_verdicts tool."
        )
        user = (
            f"ANSWER:\n{answer}\n\n"
            f"RETRIEVED CONTEXT:\n{_format_chunks(chunks)}"
        )
        tool = _verdict_list_tool(
            "submit_faithfulness_verdicts",
            "Submit per-claim faithfulness verdicts.",
            ["yes", "no", "idk"],
        )
        try:
            args = await _judge_with_tool(judge, system, user, tool)
        except RuntimeError as e:
            return Score(value=0.0, explanation=f"Judge failure: {e}")
        verdicts = args.get("verdicts") or []
        value = _faithfulness_score(verdicts)
        explanation = (
            f"faithfulness = {value:.2f} "
            f"({sum(1 for v in verdicts if v.get('verdict') in ('yes', 'idk'))}/{len(verdicts)} claims grounded)\n"
            + _summarise_verdicts(verdicts)
        )
        return Score(
            value=value,
            explanation=explanation,
            metadata={"verdicts": verdicts},
        )

    return score


@scorer(metrics=[mean(), stderr()])
def answer_relevancy(judge_model: Optional[str] = None):
    """Fraction of statements in the answer that address the question.

    Doesn't look at retrieval_context — just question vs answer. Catches
    answers that ramble about adjacent topics.
    """
    async def score(state, target):
        judge = _resolve_judge(judge_model)
        question = str(state.input)
        answer = state.output.completion if state.output else ""
        if not answer.strip():
            return Score(value=0.0, explanation="No answer produced.")

        system = (
            "You are a relevance judge. Given a question and the model's "
            "answer, extract every standalone statement from the answer and "
            "for each one decide whether it directly addresses the question:\n"
            "  yes  — the statement is on-topic and addresses what was asked\n"
            "  no   — the statement is off-topic, tangential, or unrelated\n"
            "Return the list via the submit_relevancy_verdicts tool."
        )
        user = f"QUESTION:\n{question}\n\nANSWER:\n{answer}"
        tool = _verdict_list_tool(
            "submit_relevancy_verdicts",
            "Submit per-statement relevancy verdicts.",
            ["yes", "no"],
        )
        try:
            args = await _judge_with_tool(judge, system, user, tool)
        except RuntimeError as e:
            return Score(value=0.0, explanation=f"Judge failure: {e}")
        verdicts = args.get("verdicts") or []
        value = _binary_yes_fraction(verdicts)
        explanation = (
            f"answer_relevancy = {value:.2f} "
            f"({sum(1 for v in verdicts if v.get('verdict') == 'yes')}/{len(verdicts)} statements on-topic)\n"
            + _summarise_verdicts(verdicts)
        )
        return Score(
            value=value,
            explanation=explanation,
            metadata={"verdicts": verdicts},
        )

    return score


@scorer(metrics=[mean(), stderr()])
def contextual_precision(judge_model: Optional[str] = None):
    """Precision-at-k of relevant chunks against the expected answer.

    Rewards retrievers that put relevant chunks first. Lower when
    irrelevant chunks are ranked above relevant ones. Needs both
    retrieval_context AND target (expected_output).
    """
    async def score(state, target):
        chunks = _require_retrieval_context(state, "contextual_precision")
        judge = _resolve_judge(judge_model)
        question = str(state.input)
        expected = target.text if target else ""
        if not expected.strip():
            return Score(
                value=0.0,
                explanation="contextual_precision requires an expected answer (target.text).",
            )

        system = (
            "You are a retrieval-quality judge. For each retrieved chunk in "
            "order, decide whether the chunk is USEFUL for arriving at the "
            "expected answer to the question (not whether it contains the "
            "exact answer — usefulness, including supporting context, counts):\n"
            "  yes — the chunk helps arrive at the expected answer\n"
            "  no  — the chunk is irrelevant or actively unhelpful\n"
            "Emit EXACTLY one verdict per chunk, in the same order. Use the "
            "1-based chunk_index that matches the [chunk N] header in the prompt."
        )
        user = (
            f"QUESTION:\n{question}\n\n"
            f"EXPECTED ANSWER:\n{expected}\n\n"
            f"RETRIEVED CHUNKS (in retriever rank order):\n{_format_chunks(chunks)}"
        )
        tool = _chunk_verdict_tool(
            "submit_chunk_relevance",
            "Submit one usefulness verdict per chunk, in order.",
        )
        try:
            args = await _judge_with_tool(judge, system, user, tool)
        except RuntimeError as e:
            return Score(value=0.0, explanation=f"Judge failure: {e}")
        chunk_verdicts = args.get("chunk_verdicts") or []
        value = _precision_at_k(chunk_verdicts)
        yes_count = sum(1 for v in chunk_verdicts if v.get("verdict") == "yes")
        explanation = (
            f"contextual_precision = {value:.2f} "
            f"(precision@k over {yes_count}/{len(chunks)} relevant chunks)\n"
            + "\n".join(
                f"  chunk {v.get('chunk_index')}: {v.get('verdict')} — {(v.get('reason') or '')[:80]}"
                for v in sorted(chunk_verdicts, key=lambda x: int(x.get("chunk_index", 0)))
            )
        )
        return Score(
            value=value,
            explanation=explanation,
            metadata={"chunk_verdicts": chunk_verdicts},
        )

    return score


@scorer(metrics=[mean(), stderr()])
def contextual_recall(judge_model: Optional[str] = None):
    """Fraction of expected-answer sentences backed by at least one chunk.

    Measures whether the retriever fetched enough context to support the
    golden answer. Independent of ordering.
    """
    async def score(state, target):
        chunks = _require_retrieval_context(state, "contextual_recall")
        judge = _resolve_judge(judge_model)
        expected = target.text if target else ""
        if not expected.strip():
            return Score(
                value=0.0,
                explanation="contextual_recall requires an expected answer (target.text).",
            )

        system = (
            "You are a retrieval-completeness judge. Break the expected "
            "answer into atomic sentences. For each sentence, decide "
            "whether ANY of the retrieved chunks contains the information "
            "needed to support that sentence:\n"
            "  yes — at least one chunk supports the sentence\n"
            "  no  — no chunk supports the sentence; retrieval missed it\n"
            "Return the list via the submit_recall_verdicts tool."
        )
        user = (
            f"EXPECTED ANSWER:\n{expected}\n\n"
            f"RETRIEVED CONTEXT:\n{_format_chunks(chunks)}"
        )
        tool = _verdict_list_tool(
            "submit_recall_verdicts",
            "Submit per-sentence recall verdicts.",
            ["yes", "no"],
        )
        try:
            args = await _judge_with_tool(judge, system, user, tool)
        except RuntimeError as e:
            return Score(value=0.0, explanation=f"Judge failure: {e}")
        verdicts = args.get("verdicts") or []
        value = _binary_yes_fraction(verdicts)
        explanation = (
            f"contextual_recall = {value:.2f} "
            f"({sum(1 for v in verdicts if v.get('verdict') == 'yes')}/{len(verdicts)} sentences covered)\n"
            + _summarise_verdicts(verdicts)
        )
        return Score(
            value=value,
            explanation=explanation,
            metadata={"verdicts": verdicts},
        )

    return score


@scorer(metrics=[mean(), stderr()])
def contextual_relevancy(judge_model: Optional[str] = None):
    """Fraction of chunk-level statements relevant to the question.

    Per-chunk, asks the judge to extract statements and decide which are
    relevant to the question. Captures noise — high when the retriever
    returns terse chunks tightly scoped to the query.
    """
    async def score(state, target):
        chunks = _require_retrieval_context(state, "contextual_relevancy")
        judge = _resolve_judge(judge_model)
        question = str(state.input)

        system = (
            "You are a context-relevance judge. Given a question and a list "
            "of retrieved chunks, extract every standalone statement that "
            "appears across the chunks and for each one decide whether it is "
            "RELEVANT to answering the question:\n"
            "  yes — the statement helps answer the question\n"
            "  no  — the statement is off-topic noise (boilerplate, unrelated facts, etc.)\n"
            "Include claim text verbatim so we can audit later. Return the "
            "list via the submit_chunk_statements tool."
        )
        user = (
            f"QUESTION:\n{question}\n\n"
            f"RETRIEVED CONTEXT:\n{_format_chunks(chunks)}"
        )
        tool = _verdict_list_tool(
            "submit_chunk_statements",
            "Submit per-statement relevancy verdicts for the chunk contents.",
            ["yes", "no"],
        )
        try:
            args = await _judge_with_tool(judge, system, user, tool)
        except RuntimeError as e:
            return Score(value=0.0, explanation=f"Judge failure: {e}")
        verdicts = args.get("verdicts") or []
        value = _binary_yes_fraction(verdicts)
        explanation = (
            f"contextual_relevancy = {value:.2f} "
            f"({sum(1 for v in verdicts if v.get('verdict') == 'yes')}/{len(verdicts)} chunk statements relevant)\n"
            + _summarise_verdicts(verdicts)
        )
        return Score(
            value=value,
            explanation=explanation,
            metadata={"verdicts": verdicts},
        )

    return score


@scorer(metrics=[mean(), stderr()])
def hallucination(judge_model: Optional[str] = None):
    """1 minus the fraction of answer sentences contradicted by chunks.

    Inverted from DeepEval's raw "hallucination rate" so HIGHER = MORE
    GROUNDED, matching the viewer's color scale (green/high = good,
    red/low = bad). A score of 1.0 means no sentence in the answer is
    contradicted by the retrieved context.
    """
    async def score(state, target):
        chunks = _require_retrieval_context(state, "hallucination")
        judge = _resolve_judge(judge_model)
        answer = state.output.completion if state.output else ""
        if not answer.strip():
            return Score(value=0.0, explanation="No answer produced.")

        system = (
            "You are a hallucination detector. Break the model's answer "
            "into atomic factual sentences. For each sentence, decide "
            "whether the retrieved context CONTRADICTS it (the answer says "
            "X, but the context says not-X or implies otherwise):\n"
            "  yes — the context contradicts the sentence (hallucinated)\n"
            "  no  — the context does not contradict (consistent or unaddressed)\n"
            "Return the list via the submit_hallucination_verdicts tool."
        )
        user = (
            f"ANSWER:\n{answer}\n\n"
            f"RETRIEVED CONTEXT:\n{_format_chunks(chunks)}"
        )
        tool = _verdict_list_tool(
            "submit_hallucination_verdicts",
            "Submit per-sentence hallucination verdicts.",
            ["yes", "no"],
        )
        try:
            args = await _judge_with_tool(judge, system, user, tool)
        except RuntimeError as e:
            return Score(value=0.0, explanation=f"Judge failure: {e}")
        verdicts = args.get("verdicts") or []
        contradiction_rate = _binary_yes_fraction(verdicts)
        value = 1.0 - contradiction_rate
        explanation = (
            f"hallucination (groundedness) = {value:.2f} "
            f"({sum(1 for v in verdicts if v.get('verdict') == 'no')}/{len(verdicts)} sentences consistent; "
            f"contradiction_rate={contradiction_rate:.2f})\n"
            + _summarise_verdicts(verdicts)
        )
        return Score(
            value=value,
            explanation=explanation,
            metadata={
                "verdicts": verdicts,
                "contradiction_rate": contradiction_rate,
            },
        )

    return score
