"""DeepEval-style RAG scorers for Inspect AI (ported from DeepEval v4.0.4).

These are faithful ports of DeepEval's RAG-triad metrics — the QAG
(Question-Answer-Generation) pattern: an LLM extracts atomic units
(claims / statements / sentences), a second pass assigns a verdict to
each, and the score is a ratio of the verdicts. The prompts below are
copied verbatim from DeepEval's templates so our scores track theirs;
the only thing that differs is the execution shell — we run the judge
through Inspect AI's ``get_model`` (forced tool output) rather than
DeepEval's free-JSON parsing, which keeps Bedrock cost/OTLP capture
working.

Five metrics, mirroring DeepEval's RAG suite exactly:

- ``faithfulness``          — 3 calls: truths(context) → claims(answer) →
  verdict each claim vs truths. Score = non-contradicting / total claims.
- ``answer_relevancy``      — 2 calls: statements(answer) → verdict each vs
  question. Score = relevant / total statements.
- ``contextual_precision``  — 1 call: per-node relevance vs expected answer.
  Score = weighted precision-at-k (rewards relevant nodes ranked early).
- ``contextual_recall``     — 1 call: each expected-answer sentence
  attributable to context? Score = attributable / total sentences.
- ``contextual_relevancy``  — N calls (one per chunk): extract statements
  from the chunk + verdict each vs question. Score = relevant / total
  across all chunks.

NOT ported: DeepEval's ``HallucinationMetric``. It uses the ground-truth
``context`` field (not ``retrieval_context``) and is a factual-correctness
check, not a RAG-pipeline metric. Groundedness-against-retrieved-chunks is
already covered by ``faithfulness``; factual-correctness-against-truth is
covered by the jury scorer's correctness criterion.

Judge selection: each scorer factory accepts ``judge_model``. The generated
task file calls :func:`configure_judge` once at import so scorers default to
the same judge configured for the jury. Override per-scorer with
``faithfulness(judge_model=...)``.
"""

from __future__ import annotations

from typing import List, Optional

from inspect_ai.model import ChatMessageSystem, ChatMessageUser, get_model
from inspect_ai.scorer import Score, mean, scorer, stderr
from inspect_ai.solver import solver
from inspect_ai.tool._tool_info import ToolInfo
from inspect_ai.tool._tool_params import ToolParams


_DEFAULT_JUDGE_MODEL: Optional[str] = None

# Minimal system framing — DeepEval puts everything in one user prompt and
# parses free JSON. We force structured output via a tool instead, so the
# system message just tells the judge to use the tool. The actual
# instructions live in the verbatim DeepEval prompt (the user message).
_SYSTEM = (
    "You are a meticulous evaluation judge. Follow the instructions exactly "
    "and return your answer by calling the provided tool — one entry per item, "
    "in order, with no extra commentary."
)


def configure_judge(model_id: str) -> None:
    """Set the default judge model for RAG scorers in this process."""
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
            f"retrieval_context must be a list[str]; got {type(rc).__name__}."
        )
    return rc


def _format_chunks(chunks: List[str]) -> str:
    return "\n".join(f"[chunk {i + 1}] {c}" for i, c in enumerate(chunks))


# ---------------------------------------------------------------------------
# RAG-aware solver: injects retrieval_context into the candidate model's
# prompt before generation. Supports a {context} placeholder for prompt
# fidelity; otherwise wraps with a generic template.
# ---------------------------------------------------------------------------

RAG_PROMPT_TEMPLATE = (
    "Answer the following question using ONLY the provided context. "
    "If the context does not contain the answer, say so explicitly.\n\n"
    "CONTEXT:\n{context}\n\n"
    "QUESTION: {question}"
)

CONTEXT_PLACEHOLDER = "{context}"


@solver
def rag_prompt_solver():
    """Inject retrieved chunks into the candidate model's prompt.

    If the user's prompt template contains ``{context}``, the formatted
    chunks are substituted there (full prompt fidelity). Otherwise the
    user message is wrapped with :data:`RAG_PROMPT_TEMPLATE`. No-op when
    ``state.metadata['retrieval_context']`` is absent.
    """
    async def solve(state, generate):
        chunks = (state.metadata or {}).get("retrieval_context")
        if not chunks:
            return state
        formatted = _format_chunks(list(chunks))
        for i in range(len(state.messages) - 1, -1, -1):
            msg = state.messages[i]
            if getattr(msg, "role", None) != "user":
                continue
            existing = msg.text if hasattr(msg, "text") else str(getattr(msg, "content", ""))
            if CONTEXT_PLACEHOLDER in existing:
                new_content = existing.replace(CONTEXT_PLACEHOLDER, formatted)
            else:
                new_content = RAG_PROMPT_TEMPLATE.format(
                    context=formatted, question=existing
                )
            state.messages[i] = ChatMessageUser(content=new_content)
            break
        return state

    return solve


# ---------------------------------------------------------------------------
# Judge call helpers — force structured output via a tool, return the list.
# ---------------------------------------------------------------------------


async def _call_tool(judge_model: str, prompt: str, tool: ToolInfo) -> dict:
    """Run one tool-forced judge call. Returns the tool's arguments dict.

    Raises RuntimeError if the judge didn't call the tool (caller turns
    that into a 0.0 score rather than crashing the eval).
    """
    judge = get_model(judge_model)
    result = await judge.generate(
        [ChatMessageSystem(content=_SYSTEM), ChatMessageUser(content=prompt)],
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


def _string_list_tool(name: str, key: str, description: str) -> ToolInfo:
    """Tool that returns {key: [str, ...]} — for truths/claims/statements."""
    return ToolInfo(
        name=name,
        description=description,
        parameters=ToolParams(
            type="object",
            properties={
                key: {
                    "type": "array",
                    "description": f"List of {key}.",
                    "items": {"type": "string"},
                }
            },
            required=[key],
        ),
    )


def _verdict_tool(
    name: str,
    description: str,
    verdict_values: List[str],
    extra_props: Optional[dict] = None,
    required_extra: Optional[List[str]] = None,
) -> ToolInfo:
    """Tool that returns {verdicts: [{verdict, reason, ...}]}.

    ``verdict_values`` is the enum of allowed verdicts (e.g. yes/no/idk).
    ``extra_props`` adds item fields (e.g. ``statement`` for relevancy).
    """
    item_props = {
        "verdict": {
            "type": "string",
            "enum": verdict_values,
            "description": f"One of {verdict_values}.",
        },
        "reason": {
            "type": "string",
            "description": "Justification (required for non-affirmative verdicts).",
        },
    }
    if extra_props:
        item_props.update(extra_props)
    required = ["verdict"] + (required_extra or [])
    return ToolInfo(
        name=name,
        description=description,
        parameters=ToolParams(
            type="object",
            properties={
                "verdicts": {
                    "type": "array",
                    "description": (
                        "One entry per item, in the SAME ORDER as the items "
                        "given. Length MUST equal the number of items."
                    ),
                    "items": {
                        "type": "object",
                        "properties": item_props,
                        "required": required,
                    },
                }
            },
            required=["verdicts"],
        ),
    )


# ---------------------------------------------------------------------------
# Score formulas (pure functions; tested in tests/test_rag_scorers.py).
# ---------------------------------------------------------------------------


def _fraction(numer: int, denom: int) -> float:
    return numer / denom if denom > 0 else 0.0


def _not_no_fraction(verdicts: List[dict]) -> float:
    """DeepEval faithfulness/answer_relevancy: yes AND idk count (only 'no' fails)."""
    if not verdicts:
        return 1.0  # DeepEval returns 1 when there are no claims/statements
    good = sum(1 for v in verdicts if str(v.get("verdict", "")).strip().lower() != "no")
    return _fraction(good, len(verdicts))


def _yes_fraction(verdicts: List[dict]) -> float:
    """DeepEval contextual_recall/relevancy: only explicit 'yes' counts."""
    if not verdicts:
        return 0.0
    yes = sum(1 for v in verdicts if str(v.get("verdict", "")).strip().lower() == "yes")
    return _fraction(yes, len(verdicts))


def _weighted_precision_at_k(verdicts: List[dict]) -> float:
    """DeepEval contextual_precision: order-sensitive precision@k.

    For each relevant node at rank k (1-based), add (relevant-so-far / k),
    divide by total relevant. Verdicts are in retrieval rank order.
    """
    if not verdicts:
        return 0.0
    node_relevant = [
        1 if str(v.get("verdict", "")).strip().lower() == "yes" else 0
        for v in verdicts
    ]
    sum_weighted = 0.0
    relevant_so_far = 0
    for k, is_rel in enumerate(node_relevant, start=1):
        if is_rel:
            relevant_so_far += 1
            sum_weighted += (relevant_so_far / k) * is_rel
    if relevant_so_far == 0:
        return 0.0
    return sum_weighted / relevant_so_far


def _summarise(verdicts: List[dict], limit: int = 6) -> str:
    if not verdicts:
        return "(no verdicts returned by judge)"
    lines = []
    for v in verdicts[:limit]:
        verdict = v.get("verdict", "?")
        label = v.get("statement") or v.get("claim") or v.get("reason") or ""
        lines.append(f"  [{verdict}] {str(label)[:90]}")
    if len(verdicts) > limit:
        lines.append(f"  ... ({len(verdicts) - limit} more)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Verbatim DeepEval prompts (v4.0.4). Sentinel placeholders (__X__) are used
# instead of {x} so the JSON examples' braces survive untouched.
# ---------------------------------------------------------------------------

_TRUTHS_PROMPT = """Based on the given text, please generate a comprehensive list of FACTUAL, undisputed truths, that can inferred from the provided text.
These truths, MUST BE COHERENT. They must NOT be taken out of context.

Example:
Example Text:
"Albert Einstein, the genius often associated with wild hair and mind-bending theories, famously won the Nobel Prize in Physics—though not for his groundbreaking work on relativity, as many assume. Instead, in 1968, he was honored for his discovery of the photoelectric effect, a phenomenon that laid the foundation for quantum mechanics."

Example truths: ["Einstein won the noble prize for his discovery of the photoelectric effect in 1968.", "The photoelectric effect is a phenomenon that laid the foundation for quantum mechanics."]
===== END OF EXAMPLE ======

IMPORTANT: Only include truths that are factual, BUT IT DOESN'T MATTER IF THEY ARE FACTUALLY CORRECT.

Text:
__TEXT__
"""

_CLAIMS_PROMPT = """Based on the given text, please extract a comprehensive list of FACTUAL, undisputed truths, that can inferred from the provided actual AI output.
These truths, MUST BE COHERENT, and CANNOT be taken out of context.

Example Text:
"Albert Einstein, the genius often associated with wild hair and mind-bending theories, famously won the Nobel Prize in Physics—though not for his groundbreaking work on relativity, as many assume. Instead, in 1968, he was honored for his discovery of the photoelectric effect, a phenomenon that laid the foundation for quantum mechanics."

Example claims: ["Einstein won the noble prize for his discovery of the photoelectric effect in 1968.", "The photoelectric effect is a phenomenon that laid the foundation for quantum mechanics."]
===== END OF EXAMPLE ======

IMPORTANT: Only include claims that are factual, BUT IT DOESN'T MATTER IF THEY ARE FACTUALLY CORRECT. The claims you extract should include the full context it was presented in, NOT cherry picked facts. You should NOT include any prior knowledge, and take the text at face value when extracting claims. You should be aware that it is an AI that is outputting these claims.

AI Output:
__TEXT__
"""

_FAITHFULNESS_VERDICTS_PROMPT = """Based on the given claims, which is a list of strings, generate a list indicating whether EACH claim contradicts any facts in the retrieval context. For each claim provide a 'verdict' and (when not 'yes') a 'reason'.
The 'verdict' should STRICTLY be either 'yes', 'no', or 'idk', which states whether the given claim agrees with the context.

Generate ONE verdict per claim - the number of verdicts MUST equal the number of claims, in the same order.
No 'reason' needed for 'yes' verdicts.
Only use 'no' if the retrieval context DIRECTLY CONTRADICTS the claim - never use prior knowledge.
Use 'idk' for claims not backed up by the context OR factually incorrect but non-contradictory - do not assume your knowledge.
Vague/speculative language in claims (e.g. 'may have', 'possibility') does NOT count as a contradiction.

Retrieval Contexts:
__CONTEXT__

Claims:
__CLAIMS__
"""

_STATEMENTS_PROMPT = """Given the text, breakdown and generate a list of statements presented. Ambiguous statements and single words can be considered as statements, but only if outside of a coherent statement.

Example text:
Our new laptop model features a high-resolution Retina display for crystal-clear visuals. It also includes a fast-charging battery, giving you up to 12 hours of usage on a single charge. For security, we've added fingerprint authentication and an encrypted SSD. Plus, every purchase comes with a one-year warranty and 24/7 customer support.

Example statements: ["The new laptop model has a high-resolution Retina display.", "It includes a fast-charging battery with up to 12 hours of usage.", "Security features include fingerprint authentication and an encrypted SSD.", "Every purchase comes with a one-year warranty.", "24/7 customer support is included."]
===== END OF EXAMPLE ======

Text:
__TEXT__
"""

_ANSWER_RELEVANCY_VERDICTS_PROMPT = """For the provided list of statements, determine whether each statement is relevant to address the input.
For each statement provide a 'verdict' and (when not 'yes') a 'reason'. The statements are from an AI's actual output.

Generate ONE verdict per statement - the number of verdicts MUST equal the number of statements, in the same order.
'verdict' must be STRICTLY 'yes', 'no', or 'idk':
- 'yes': statement is relevant to addressing the input
- 'no': statement is irrelevant to the input
- 'idk': statement is ambiguous (not directly relevant but could be supporting information)
Provide 'reason' ONLY for 'no' or 'idk' verdicts.

Input:
__INPUT__

Statements:
__STATEMENTS__
"""

_CONTEXTUAL_PRECISION_VERDICTS_PROMPT = """Given the input, expected output, and retrieval context, please generate a list to determine whether each node in the retrieval context was remotely useful in arriving at the expected output. For each node provide a 'verdict' ('yes' or 'no') and a 'reason' that quotes parts of the context.

Example Retrieval Context: ["Einstein won the Nobel Prize for his discovery of the photoelectric effect", "He won the Nobel Prize in 1968.", "There was a cat."]
Example Input: "Who won the Nobel Prize in 1968 and for what?"
Example Expected Output: "Einstein won the Nobel Prize in 1968 for his discovery of the photoelectric effect."
Example verdicts: [{"reason": "It clearly addresses the question...", "verdict": "yes"}, {"reason": "The text verifies the prize was won in 1968.", "verdict": "yes"}, {"reason": "'There was a cat' is not relevant.", "verdict": "no"}]

Generate a verdict for EACH node - the number of verdicts SHOULD BE STRICTLY EQUAL to the number of nodes, in the same order.

Input:
__INPUT__

Expected output:
__EXPECTED_OUTPUT__

Retrieval Context:
__CONTEXT__
"""

_CONTEXTUAL_RECALL_VERDICTS_PROMPT = """For EACH sentence in the given expected output below, determine whether the sentence can be attributed to the nodes of retrieval contexts. For each sentence provide a 'verdict' and a 'reason'.
The 'verdict' should STRICTLY be either 'yes' or 'no'. Answer 'yes' if the sentence can be attributed to any parts of the retrieval context, else answer 'no'.
In the 'reason', aim to include the node(s) count in the retrieval context (e.g. 1st node, 2nd node) attributed to the sentence, and quote the specific part of the retrieval context, kept extremely concise.

Generate a verdict for EACH sentence - the number of verdicts SHOULD BE STRICTLY EQUAL to the number of sentences in the expected output, in the same order.

Expected Output:
__EXPECTED_OUTPUT__

Retrieval Context:
__CONTEXT__
"""

_CONTEXTUAL_RELEVANCY_VERDICTS_PROMPT = """Based on the input and context, please generate a list of verdicts to indicate whether each statement found in the context is relevant to the provided input. Each verdict has a 'verdict', a 'statement', and (only when 'no') a 'reason'.
You should first extract statements found in the context, which are high level information found in the context, before deciding on a verdict for each statement.
The 'verdict' should STRICTLY be either 'yes' or 'no', and states whether the statement is relevant to the input.
Provide a 'reason' ONLY IF the verdict is 'no'. You MUST quote the irrelevant parts of the statement to back up your reason.
If the provided context contains no actual content or statements, give 'no' as the verdict, put the context into 'statement', and "No statements found in provided context." into 'reason'.

Example Context: "Einstein won the Nobel Prize for his discovery of the photoelectric effect. He won the Nobel Prize in 1968. There was a cat."
Example Input: "What were some of Einstein's achievements?"
Example verdicts: [{"statement": "Einstein won the Nobel Prize for his discovery of the photoelectric effect in 1968", "verdict": "yes"}, {"statement": "There was a cat.", "reason": "The context mentioned 'There was a cat' which has nothing to do with Einstein's achievements.", "verdict": "no"}]

Input:
__INPUT__

Context:
__CONTEXT__
"""


# ---------------------------------------------------------------------------
# Scorers
# ---------------------------------------------------------------------------


@scorer(metrics=[mean(), stderr()])
def faithfulness(judge_model: Optional[str] = None):
    """Fraction of answer claims that don't contradict the retrieved context.

    Ported from DeepEval (3 stages): extract truths from context, extract
    claims from the answer, then verdict each claim against the truths.
    yes + idk both count as faithful; only 'no' (direct contradiction) fails.
    """
    async def score(state, target):
        chunks = _require_retrieval_context(state, "faithfulness")
        judge = _resolve_judge(judge_model)
        answer = state.output.completion if state.output else ""
        if not answer.strip():
            return Score(value=0.0, explanation="No answer produced.")
        try:
            truths_args = await _call_tool(
                judge,
                _TRUTHS_PROMPT.replace("__TEXT__", _format_chunks(chunks)),
                _string_list_tool("submit_truths", "truths", "Extract factual truths from the context."),
            )
            truths = [str(t) for t in (truths_args.get("truths") or [])]

            claims_args = await _call_tool(
                judge,
                _CLAIMS_PROMPT.replace("__TEXT__", answer),
                _string_list_tool("submit_claims", "claims", "Extract claims from the AI output."),
            )
            claims = [str(c) for c in (claims_args.get("claims") or [])]

            if not claims:
                return Score(value=1.0, explanation="No claims extracted from the answer.")

            verdict_args = await _call_tool(
                judge,
                _FAITHFULNESS_VERDICTS_PROMPT
                .replace("__CONTEXT__", "\n\n".join(truths))
                .replace("__CLAIMS__", "\n".join(f"- {c}" for c in claims)),
                _verdict_tool(
                    "submit_faithfulness_verdicts",
                    "One verdict per claim: does it contradict the context?",
                    ["yes", "no", "idk"],
                ),
            )
        except RuntimeError as e:
            return Score(value=0.0, explanation=f"Judge failure: {e}")
        verdicts = verdict_args.get("verdicts") or []
        value = _not_no_fraction(verdicts)
        n_good = sum(1 for v in verdicts if str(v.get("verdict", "")).lower() != "no")
        explanation = (
            f"faithfulness = {value:.2f} ({n_good}/{len(verdicts)} claims not contradicted; "
            f"{len(truths)} truths, {len(claims)} claims)\n" + _summarise(verdicts)
        )
        return Score(value=value, explanation=explanation,
                     metadata={"truths": truths, "claims": claims, "verdicts": verdicts})

    return score


@scorer(metrics=[mean(), stderr()])
def answer_relevancy(judge_model: Optional[str] = None):
    """Fraction of answer statements relevant to the question.

    Ported from DeepEval (2 stages): extract statements from the answer,
    verdict each against the question. yes + idk count as relevant.
    Doesn't use retrieval_context.
    """
    async def score(state, target):
        judge = _resolve_judge(judge_model)
        question = str(state.input)
        answer = state.output.completion if state.output else ""
        if not answer.strip():
            return Score(value=0.0, explanation="No answer produced.")
        try:
            stmt_args = await _call_tool(
                judge,
                _STATEMENTS_PROMPT.replace("__TEXT__", answer),
                _string_list_tool("submit_statements", "statements", "Break the answer into statements."),
            )
            statements = [str(s) for s in (stmt_args.get("statements") or [])]
            if not statements:
                return Score(value=1.0, explanation="No statements extracted from the answer.")

            verdict_args = await _call_tool(
                judge,
                _ANSWER_RELEVANCY_VERDICTS_PROMPT
                .replace("__INPUT__", question)
                .replace("__STATEMENTS__", "\n".join(f"- {s}" for s in statements)),
                _verdict_tool(
                    "submit_relevancy_verdicts",
                    "One verdict per statement: is it relevant to the input?",
                    ["yes", "no", "idk"],
                ),
            )
        except RuntimeError as e:
            return Score(value=0.0, explanation=f"Judge failure: {e}")
        verdicts = verdict_args.get("verdicts") or []
        value = _not_no_fraction(verdicts)
        n_good = sum(1 for v in verdicts if str(v.get("verdict", "")).lower() != "no")
        explanation = (
            f"answer_relevancy = {value:.2f} ({n_good}/{len(verdicts)} statements relevant)\n"
            + _summarise(verdicts)
        )
        return Score(value=value, explanation=explanation,
                     metadata={"statements": statements, "verdicts": verdicts})

    return score


@scorer(metrics=[mean(), stderr()])
def contextual_precision(judge_model: Optional[str] = None):
    """Weighted precision-at-k of retrieved nodes vs the expected answer.

    Ported from DeepEval (1 call): verdict each node relevant/not (in rank
    order), then order-sensitive precision@k — rewards relevant nodes
    ranked early. Needs retrieval_context AND target (expected answer).
    """
    async def score(state, target):
        chunks = _require_retrieval_context(state, "contextual_precision")
        judge = _resolve_judge(judge_model)
        question = str(state.input)
        expected = target.text if target else ""
        if not expected.strip():
            return Score(value=0.0,
                         explanation="contextual_precision requires an expected answer (target).")
        try:
            args = await _call_tool(
                judge,
                _CONTEXTUAL_PRECISION_VERDICTS_PROMPT
                .replace("__INPUT__", question)
                .replace("__EXPECTED_OUTPUT__", expected)
                .replace("__CONTEXT__", _format_chunks(chunks)),
                _verdict_tool(
                    "submit_node_verdicts",
                    "One verdict per node, in rank order: useful for the expected output?",
                    ["yes", "no"],
                ),
            )
        except RuntimeError as e:
            return Score(value=0.0, explanation=f"Judge failure: {e}")
        verdicts = args.get("verdicts") or []
        value = _weighted_precision_at_k(verdicts)
        n_rel = sum(1 for v in verdicts if str(v.get("verdict", "")).lower() == "yes")
        explanation = (
            f"contextual_precision = {value:.2f} (precision@k over {n_rel}/{len(chunks)} relevant nodes)\n"
            + _summarise(verdicts)
        )
        return Score(value=value, explanation=explanation, metadata={"verdicts": verdicts})

    return score


@scorer(metrics=[mean(), stderr()])
def contextual_recall(judge_model: Optional[str] = None):
    """Fraction of expected-answer sentences attributable to the context.

    Ported from DeepEval (1 call): verdict each expected-answer sentence
    yes/no on whether the context supports it. Needs retrieval_context
    AND target. Only explicit 'yes' counts.
    """
    async def score(state, target):
        chunks = _require_retrieval_context(state, "contextual_recall")
        judge = _resolve_judge(judge_model)
        expected = target.text if target else ""
        if not expected.strip():
            return Score(value=0.0,
                         explanation="contextual_recall requires an expected answer (target).")
        try:
            args = await _call_tool(
                judge,
                _CONTEXTUAL_RECALL_VERDICTS_PROMPT
                .replace("__EXPECTED_OUTPUT__", expected)
                .replace("__CONTEXT__", _format_chunks(chunks)),
                _verdict_tool(
                    "submit_recall_verdicts",
                    "One verdict per expected-output sentence: attributable to context?",
                    ["yes", "no"],
                ),
            )
        except RuntimeError as e:
            return Score(value=0.0, explanation=f"Judge failure: {e}")
        verdicts = args.get("verdicts") or []
        value = _yes_fraction(verdicts)
        n_yes = sum(1 for v in verdicts if str(v.get("verdict", "")).lower() == "yes")
        explanation = (
            f"contextual_recall = {value:.2f} ({n_yes}/{len(verdicts)} sentences attributable)\n"
            + _summarise(verdicts)
        )
        return Score(value=value, explanation=explanation, metadata={"verdicts": verdicts})

    return score


@scorer(metrics=[mean(), stderr()])
def contextual_relevancy(judge_model: Optional[str] = None):
    """Fraction of context statements relevant to the question.

    Ported from DeepEval: ONE judge call PER chunk — extract the chunk's
    statements and verdict each relevant/not to the question — then
    aggregate relevant/total across all chunks. Only explicit 'yes' counts.
    """
    async def score(state, target):
        chunks = _require_retrieval_context(state, "contextual_relevancy")
        judge = _resolve_judge(judge_model)
        question = str(state.input)
        all_verdicts: List[dict] = []
        try:
            for chunk in chunks:
                args = await _call_tool(
                    judge,
                    _CONTEXTUAL_RELEVANCY_VERDICTS_PROMPT
                    .replace("__INPUT__", question)
                    .replace("__CONTEXT__", chunk),
                    _verdict_tool(
                        "submit_statement_verdicts",
                        "Extract statements from THIS chunk and verdict each vs the input.",
                        ["yes", "no"],
                        extra_props={
                            "statement": {
                                "type": "string",
                                "description": "The statement extracted from the context.",
                            }
                        },
                        required_extra=["statement"],
                    ),
                )
                all_verdicts.extend(args.get("verdicts") or [])
        except RuntimeError as e:
            return Score(value=0.0, explanation=f"Judge failure: {e}")
        value = _yes_fraction(all_verdicts)
        n_yes = sum(1 for v in all_verdicts if str(v.get("verdict", "")).lower() == "yes")
        explanation = (
            f"contextual_relevancy = {value:.2f} ({n_yes}/{len(all_verdicts)} statements relevant "
            f"across {len(chunks)} chunks)\n" + _summarise(all_verdicts)
        )
        return Score(value=value, explanation=explanation, metadata={"verdicts": all_verdicts})

    return score
