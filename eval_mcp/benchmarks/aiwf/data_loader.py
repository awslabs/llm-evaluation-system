"""Vendored aiewf data + the five conference tools, as Inspect primitives.

Everything here is data loading and schema translation — no model calls. The
JSON/txt files under ``data/`` are upstream's, converted once at vendor time:

- ``turns.json`` — upstream's ``benchmarks/_shared/turns.py`` list, with the
  ``audio_file`` key dropped (speech-only) and an explicit ``index`` added.
- ``kb_medium.txt`` / ``kb_long.txt`` — the two knowledge bases verbatim.
- ``system_preamble.txt`` / ``system_tools_section.txt`` — the two literal
  blocks upstream concatenates around the knowledge base, verbatim. Kept as
  separate files because the prompt is ``preamble + kb + tools_section`` and
  only the middle differs between the two variants.
"""

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from inspect_ai.tool import Tool, ToolDef

_DATA = Path(__file__).parent / "data"

# Run the same conversation on inputs that did NOT come from a keyboard — e.g.
# STT transcripts of the spoken form of each turn, for choosing a model to sit
# behind a speech pipeline. Only ``input`` may differ: the goldens and expected
# tool calls define the task, so the override is checked against the vendored
# script and a mismatch is fatal. Silently scoring different targets while
# reporting the benchmark's name is worse than failing.
_TURNS_ENV = "EVAL_MCP_AIWF_TURNS_FILE"

# Variant name → knowledge-base file. Upstream's two benchmarks differ ONLY in
# this file; turns, tools and prompt scaffolding are shared.
AIWF_TASKS: Dict[str, str] = {
    "aiwf_medium_context": "kb_medium.txt",
    "aiwf_long_context": "kb_long.txt",
}


@dataclass(frozen=True)
class Turn:
    """One scripted user utterance and what a correct response looks like.

    ``required_function_call`` is upstream's golden tool call: ``{"name": ...,
    "args": {...}}`` on the 6 turns that expect one, ``None`` on the other 24
    (where calling anything is itself a failure).
    """

    index: int
    input: str
    golden_text: str
    required_function_call: Optional[Dict[str, Any]]

    @property
    def expected_tool(self) -> Optional[str]:
        return (self.required_function_call or {}).get("name")

    @property
    def expected_args(self) -> Dict[str, Any]:
        return (self.required_function_call or {}).get("args") or {}


def _vendored_turns() -> List[Turn]:
    raw = json.loads((_DATA / "turns.json").read_text(encoding="utf-8"))
    return [Turn(**t) for t in raw]


def _check_same_task(override: List[Turn]) -> None:
    """Fail unless `override` differs from the vendored script ONLY in `input`."""
    base = _vendored_turns()
    if len(override) != len(base):
        raise ValueError(
            f"{_TURNS_ENV}: expected {len(base)} turns, got {len(override)}"
        )
    for got, want in zip(override, base):
        if got.index != want.index:
            raise ValueError(f"{_TURNS_ENV}: turn order changed at index {want.index}")
        if got.golden_text != want.golden_text:
            raise ValueError(
                f"{_TURNS_ENV}: golden_text edited on turn {want.index}. Only "
                "`input` may differ — the goldens define the task being scored."
            )
        if got.required_function_call != want.required_function_call:
            raise ValueError(
                f"{_TURNS_ENV}: required_function_call edited on turn "
                f"{want.index}. Only `input` may differ."
            )


@lru_cache(maxsize=1)
def turns() -> List[Turn]:
    """The 30 conversation turns, in order.

    ``EVAL_MCP_AIWF_TURNS_FILE`` swaps in user inputs from elsewhere (e.g. STT
    transcripts) while holding the scored targets fixed — see ``_TURNS_ENV``.
    """
    override = os.environ.get(_TURNS_ENV)
    if not override:
        return _vendored_turns()
    path = Path(override)
    if not path.is_file():
        raise FileNotFoundError(f"{_TURNS_ENV}={override!r} is not a readable file")
    got = [Turn(**t) for t in json.loads(path.read_text(encoding="utf-8"))]
    _check_same_task(got)
    return got


@lru_cache(maxsize=None)
def knowledge_base(variant: str) -> str:
    """The knowledge base for one variant (~12K tokens medium, ~40K long)."""
    try:
        filename = AIWF_TASKS[variant]
    except KeyError:
        raise ValueError(
            f"Unknown aiwf variant {variant!r}. Expected one of: "
            f"{sorted(AIWF_TASKS)}"
        ) from None
    return (_DATA / filename).read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def system_prompt(variant: str) -> str:
    """Upstream's system instruction: preamble + knowledge base + tool docs.

    Reproduces ``system_instruction = _PREAMBLE + _load_knowledge_base() +
    _TOOLS_SECTION`` from upstream's ``prompts/system.py``. The tools section
    is prose *about* the tools (kept because the benchmark's instruction-
    following criteria reference it); the actual callable schemas come from
    ``tool_defs()`` and are passed to the model as real tools.
    """
    preamble = (_DATA / "system_preamble.txt").read_text(encoding="utf-8")
    tools_section = (_DATA / "system_tools_section.txt").read_text(encoding="utf-8")
    return preamble + knowledge_base(variant) + tools_section


# ---------------------------------------------------------------------------
# The five conference tools.
#
# Upstream registers a single catch-all handler that returns {"status":
# "success"} for every call and treats ``end_session`` specially. We do the
# same: nothing is actually submitted anywhere, and what's being measured is
# whether the model calls the right function with the right arguments — the
# result only has to be plausible enough for the conversation to continue.
#
# Built via ToolDef rather than @tool so the JSON schema stays a faithful
# transcription of upstream's FunctionSchema definitions (names, descriptions
# and required-ness all affect whether a model calls them correctly, so they're
# part of the benchmark, not an implementation detail).
# ---------------------------------------------------------------------------

_SUCCESS = {"status": "success"}


async def _end_session() -> Dict[str, str]:
    return _SUCCESS


async def _submit_dietary_request(name: str, dietary_preference: str) -> Dict[str, str]:
    return _SUCCESS


async def _submit_session_suggestion(name: str, suggestion_text: str) -> Dict[str, str]:
    return _SUCCESS


async def _vote_for_session(name: str, session_id: str) -> Dict[str, str]:
    return _SUCCESS


async def _request_tech_support(name: str, issue_description: str) -> Dict[str, str]:
    return _SUCCESS


def tool_defs() -> List[Tool]:
    """The five tools, in upstream's declaration order."""
    return [
        ToolDef(
            _end_session,
            name="end_session",
            description="End the current session.",
            parameters={},
        ).as_tool(),
        ToolDef(
            _submit_dietary_request,
            name="submit_dietary_request",
            description="Submit a dietary request for event meals.",
            parameters={
                "name": "The name of the person making the request.",
                "dietary_preference": (
                    "The dietary preference (e.g., vegetarian, gluten-free, vegan)."
                ),
            },
        ).as_tool(),
        ToolDef(
            _submit_session_suggestion,
            name="submit_session_suggestion",
            description="Submit a suggestion for a new session or talk at the event.",
            parameters={
                "name": "The name of the person making the suggestion.",
                "suggestion_text": "The text of the session suggestion.",
            },
        ).as_tool(),
        ToolDef(
            _vote_for_session,
            name="vote_for_session",
            description="Vote for an existing session to show your interest.",
            parameters={
                "name": "The name of the person voting.",
                "session_id": (
                    "The Session ID of the session being voted for. "
                    "The Session ID is a number."
                ),
            },
        ).as_tool(),
        ToolDef(
            _request_tech_support,
            name="request_tech_support",
            description=(
                "Request technical support for an issue at the event "
                "(e.g., WiFi problems, app issues)."
            ),
            parameters={
                "name": "The name of the person requesting support.",
                "issue_description": "A description of the technical issue.",
            },
        ).as_tool(),
    ]
