"""mcp-builder Phase 4 evaluation runner for eval-mcp.

Connects a Claude (Bedrock Sonnet) agent to the eval-mcp server via stdio,
asks it each XML question, captures the trace + final answer, and grades
the answer with a separate Sonnet judge call against the
`<answer_must_contain>` facts.

Why Bedrock rather than the eval-mcp judge:
    The point of the mcp-builder eval is independent verification of the
    MCP layer. Reusing eval-mcp's own judge infrastructure would couple
    the test to the very thing we're testing. We use Bedrock Converse
    directly with prompt caching for efficiency.

Usage:
    python -m tests.mcp_eval.runner              # all 10 questions
    python -m tests.mcp_eval.runner --question 3 # one question
    python -m tests.mcp_eval.runner --json out.json  # machine-readable result

Skip-on-no-Bedrock: the pytest wrapper at tests/test_mcp_eval.py guards
against missing AWS credentials. Run that directly if you don't want to
think about creds.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Sonnet 4.6 — fast, cheap, strong tool use. Sonnet 4.5 inference profile
# is the most-widely-available fallback if 4.6 isn't enabled in the user's
# AWS account.
AGENT_MODEL = os.environ.get(
    "EVAL_MCP_EVAL_MODEL", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)
JUDGE_MODEL = os.environ.get("EVAL_MCP_JUDGE_MODEL", AGENT_MODEL)
MAX_AGENT_TURNS = 8

QUESTIONS_PATH = Path(__file__).parent / "questions.xml"


# ---------------------------------------------------------------------------
# Question parsing
# ---------------------------------------------------------------------------


@dataclass
class Question:
    index: int
    text: str
    facts: list[str]
    expected_tools: list[str]


def load_questions(path: Path = QUESTIONS_PATH) -> list[Question]:
    tree = ET.parse(path)
    out = []
    for i, qa in enumerate(tree.findall(".//qa_pair"), start=1):
        q = (qa.find("question").text or "").strip()
        facts = [
            (f.text or "").strip()
            for f in qa.findall("answer_must_contain/fact")
            if (f.text or "").strip()
        ]
        et_node = qa.find("expected_tools")
        tools = (
            [(t.text or "").strip() for t in et_node.findall("tool")]
            if et_node is not None
            else []
        )
        out.append(Question(index=i, text=q, facts=facts, expected_tools=tools))
    return out


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class QuestionResult:
    index: int
    question: str
    answer: str
    tools_called: list[str]
    fact_results: list[tuple[str, bool]] = field(default_factory=list)
    expected_tool_satisfied: bool | None = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        if self.error:
            return False
        if not self.fact_results:
            return False
        if any(not ok for _, ok in self.fact_results):
            return False
        if self.expected_tool_satisfied is False:
            return False
        return True


# ---------------------------------------------------------------------------
# Bedrock helpers — single client, prompt-cached tool list
# ---------------------------------------------------------------------------


def _bedrock_client():
    import boto3  # imported lazily so the module loads without AWS creds

    return boto3.client(
        "bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-west-2")
    )


def _mcp_tools_to_bedrock(mcp_tools: list[Any]) -> list[dict]:
    """Convert MCP Tool objects into Bedrock Converse tool specs."""
    specs = []
    for t in mcp_tools:
        specs.append(
            {
                "toolSpec": {
                    "name": t.name,
                    "description": t.description or "",
                    "inputSchema": {"json": t.inputSchema},
                }
            }
        )
    return specs


# ---------------------------------------------------------------------------
# Agent loop — Claude answers one question using the MCP tools
# ---------------------------------------------------------------------------


async def run_question(
    session,
    bedrock,
    tool_specs: list[dict],
    question: Question,
) -> QuestionResult:
    """Have Claude attempt one question via the MCP session. Returns the
    final text answer + the trace of MCP tools the agent called."""

    system_prompt = (
        "You are evaluating the eval-mcp MCP server. Answer the user's "
        "question using only the tools available from eval-mcp when you "
        "need live data. For conceptual or workflow questions, you may "
        "answer from the tool descriptions without invoking a tool. "
        "When you are done, produce a final text response (no further "
        "tool calls). Be concise — one short paragraph is plenty."
    )

    messages: list[dict] = [
        {"role": "user", "content": [{"text": question.text}]}
    ]
    tools_called: list[str] = []
    final_answer = ""

    for _ in range(MAX_AGENT_TURNS):
        resp = bedrock.converse(
            modelId=AGENT_MODEL,
            messages=messages,
            system=[{"text": system_prompt}],
            toolConfig={"tools": tool_specs},
            inferenceConfig={"maxTokens": 1024, "temperature": 0.0},
        )
        output_msg = resp["output"]["message"]
        messages.append(output_msg)

        tool_uses = [b["toolUse"] for b in output_msg["content"] if "toolUse" in b]
        stop_reason = resp.get("stopReason")

        if not tool_uses:
            for b in output_msg["content"]:
                if "text" in b:
                    final_answer = b["text"].strip()
                    break
            break

        # Execute each tool call via the MCP session, feed results back.
        tool_result_blocks = []
        for tu in tool_uses:
            tools_called.append(tu["name"])
            try:
                result = await session.call_tool(tu["name"], tu.get("input", {}) or {})
                text = result.content[0].text if result.content else ""
            except Exception as e:
                text = json.dumps({"error": str(e)})
            tool_result_blocks.append(
                {
                    "toolResult": {
                        "toolUseId": tu["toolUseId"],
                        "content": [{"text": text[:6000]}],
                    }
                }
            )
        messages.append({"role": "user", "content": tool_result_blocks})

        if stop_reason == "end_turn":
            break

    return QuestionResult(
        index=question.index,
        question=question.text,
        answer=final_answer,
        tools_called=tools_called,
    )


# ---------------------------------------------------------------------------
# Judge — score the agent's answer against the expected facts
# ---------------------------------------------------------------------------


_JUDGE_INSTR = (
    "You are grading an answer to a technical question about an MCP server. "
    "For each fact listed below, decide whether the answer supports that "
    "fact. Be strict: the answer must clearly state the fact, not merely "
    "imply it. Respond ONLY with a JSON array of booleans, one per fact, "
    "in order. Example: [true, true, false]."
)


def grade_answer(bedrock, question: Question, answer: str) -> list[bool]:
    if not question.facts:
        return []
    prompt = (
        f"Question: {question.text}\n\n"
        f"Answer: {answer}\n\n"
        f"Facts ({len(question.facts)}):\n"
        + "\n".join(f"{i}. {f}" for i, f in enumerate(question.facts, start=1))
    )
    resp = bedrock.converse(
        modelId=JUDGE_MODEL,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        system=[{"text": _JUDGE_INSTR}],
        inferenceConfig={"maxTokens": 200, "temperature": 0.0},
    )
    text = resp["output"]["message"]["content"][0]["text"]
    m = re.search(r"\[[^\]]*\]", text)
    if not m:
        return [False] * len(question.facts)
    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError:
        return [False] * len(question.facts)
    # Coerce to length-matched bools.
    out = []
    for i in range(len(question.facts)):
        if i < len(parsed):
            out.append(bool(parsed[i]))
        else:
            out.append(False)
    return out


# ---------------------------------------------------------------------------
# Main runner — spawns eval-mcp via stdio, runs each question, grades each
# ---------------------------------------------------------------------------


async def run(question_indices: list[int] | None = None) -> list[QuestionResult]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    questions = load_questions()
    if question_indices is not None:
        questions = [q for q in questions if q.index in question_indices]

    bedrock = _bedrock_client()

    # Isolated fixture user dir so the MCP starts with a clean slate every
    # run. Auto-pull/auto-push are no-ops without an S3 bucket configured.
    with tempfile.TemporaryDirectory(prefix="eval_mcp_eval_") as tmp:
        env = {
            **os.environ,
            "EVAL_MCP_USER": "eval_test_fixture",
            "USER_STORAGE_BASE": tmp,
            "EVAL_MCP_TRANSPORT": "stdio",
        }

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "eval_mcp.server"],
            env=env,
        )

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_resp = await session.list_tools()
                tool_specs = _mcp_tools_to_bedrock(tools_resp.tools)

                results: list[QuestionResult] = []
                for q in questions:
                    try:
                        r = await run_question(session, bedrock, tool_specs, q)
                    except Exception as e:
                        r = QuestionResult(
                            index=q.index,
                            question=q.text,
                            answer="",
                            tools_called=[],
                            error=f"{type(e).__name__}: {e}",
                        )
                    if not r.error:
                        flags = grade_answer(bedrock, q, r.answer)
                        r.fact_results = list(zip(q.facts, flags))
                        if q.expected_tools:
                            r.expected_tool_satisfied = any(
                                t in r.tools_called for t in q.expected_tools
                            )
                    results.append(r)
                return results


def format_summary(results: list[QuestionResult]) -> str:
    lines = []
    passed = sum(1 for r in results if r.passed)
    lines.append(f"\n=== eval-mcp evaluation: {passed}/{len(results)} passed ===\n")
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        lines.append(f"[{status}] Q{r.index}: {r.question[:90]}{'…' if len(r.question) > 90 else ''}")
        if r.error:
            lines.append(f"        error: {r.error}")
            continue
        for fact, ok in r.fact_results:
            mark = "✓" if ok else "✗"
            lines.append(f"        {mark} {fact}")
        if r.expected_tool_satisfied is not None:
            mark = "✓" if r.expected_tool_satisfied else "✗"
            lines.append(f"        {mark} called one of expected tools")
        lines.append(f"        tools_called: {r.tools_called or 'none'}")
        if not r.answer:
            lines.append("        (no final answer text)")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description="Run the eval-mcp MCP evaluation.")
    p.add_argument(
        "--question",
        "-q",
        type=int,
        action="append",
        help="Run only the given question index (1-based). Repeatable.",
    )
    p.add_argument("--json", help="Write machine-readable results to this path.")
    args = p.parse_args()

    results = asyncio.run(run(question_indices=args.question))
    print(format_summary(results))

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                [
                    {
                        "index": r.index,
                        "question": r.question,
                        "answer": r.answer,
                        "tools_called": r.tools_called,
                        "fact_results": [
                            {"fact": f, "passed": ok} for f, ok in r.fact_results
                        ],
                        "expected_tool_satisfied": r.expected_tool_satisfied,
                        "passed": r.passed,
                        "error": r.error,
                    }
                    for r in results
                ],
                indent=2,
            )
        )

    failed = sum(1 for r in results if not r.passed)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
