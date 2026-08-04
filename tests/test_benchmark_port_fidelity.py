"""Repo-wide guard: every ported benchmark must keep its instrument verbatim.

Docs get skimmed; tests don't. This file enforces the porting contract in
CLAUDE.md ("copy the instrument, adapt the plumbing") for EVERY benchmark under
``eval_mcp/benchmarks/``, present and future. The contract needs a mechanical
check because the way it breaks is quiet: a rubric that has been reworded still
runs, still scores, and still passes every test — it just measures something
other than what the original measured.

It is deliberately structural rather than content-specific: it checks that each
port declares its provenance and vendors its prompts as data, not that any
particular prompt says any particular thing. Per-benchmark diff tests (see
``test_aiwf_benchmark.py``) pin the actual text.
"""

import re
from pathlib import Path

import pytest

BENCHMARKS_DIR = Path(__file__).parent.parent / "eval_mcp" / "benchmarks"

# A ported benchmark = a package under eval_mcp/benchmarks/ with a task module.
PORTS = sorted(
    p for p in BENCHMARKS_DIR.iterdir()
    if p.is_dir() and not p.name.startswith(("_", ".")) and (p / "task.py").exists()
) if BENCHMARKS_DIR.is_dir() else []

# Prompt-ish text that must live in data/ rather than inline in Python. Matches
# a triple-quoted literal long enough to be a prompt (not a docstring one-liner)
# whose content looks like rubric/instruction text.
_PROMPT_MARKERS = (
    "you are an expert",
    "you are a helpful",
    "you must output",
    "evaluate each turn",
    "for each turn, evaluate",
    "score the following",
    "rate the following",
    "you will judge",
    "your task is to grade",
)


_IDS = [p.name for p in PORTS]


@pytest.mark.skipif(not PORTS, reason="no ported benchmarks yet")
@pytest.mark.parametrize("port", PORTS, ids=_IDS)
def test_port_has_a_notice_with_provenance(port: Path):
    """Every port must state where it came from and pin the exact commit.

    Without a pinned SHA there is no way to re-verify fidelity later: upstream
    moves, and "we copied it from their repo" stops being checkable.
    """
    notice = port / "NOTICE.md"
    assert notice.is_file(), (
        f"{port.name} has no NOTICE.md. Every ported benchmark needs one: "
        f"upstream URL, license, pinned commit SHA, and what was copied vs "
        f"adapted vs not ported. See eval_mcp/benchmarks/aiwf/NOTICE.md."
    )
    text = notice.read_text(encoding="utf-8")
    assert re.search(r"https?://\S+", text), f"{port.name}: NOTICE.md has no upstream URL"
    assert re.search(r"\b[0-9a-f]{40}\b", text), (
        f"{port.name}: NOTICE.md must pin a full 40-char upstream commit SHA, so "
        f"the port can be re-diffed against the exact source it came from."
    )
    assert re.search(r"licen[cs]e|MIT|Apache|BSD", text, re.I), (
        f"{port.name}: NOTICE.md must state the upstream license"
    )


@pytest.mark.skipif(not PORTS, reason="no ported benchmarks yet")
@pytest.mark.parametrize("port", PORTS, ids=_IDS)
def test_port_vendors_its_data(port: Path):
    """Datasets/prompts live in data/, so fidelity is checkable by diff."""
    data = port / "data"
    assert data.is_dir() and any(data.iterdir()), (
        f"{port.name} has no data/ directory. Vendor datasets and prompts as "
        f"data files (even when upstream keeps them in Python) so a future edit "
        f"to the measuring instrument shows up in a diff."
    )


@pytest.mark.skipif(not PORTS, reason="no ported benchmarks yet")
@pytest.mark.parametrize("port", PORTS, ids=_IDS)
def test_port_does_not_inline_long_prompts_in_python(port: Path):
    """Prompts belong in data/, not in a Python string literal.

    A prompt in a .py string literal *looks* like code, and code invites
    adaptation — that framing is the trap. Keeping prompts in data files makes an
    edit to the measuring instrument show up as a data diff rather than hiding
    inside a refactor.
    """
    offenders = []
    for py in port.rglob("*.py"):
        src = py.read_text(encoding="utf-8")
        for literal in re.findall(r'"""(.*?)"""', src, re.S):
            if len(literal) < 400:
                continue  # docstrings, short templates
            low = literal.lower()
            if any(m in low for m in _PROMPT_MARKERS):
                offenders.append(f"{py.relative_to(port)} ({len(literal)} chars)")
    assert not offenders, (
        f"{port.name}: prompt-like text inlined in Python: {offenders}. Move it "
        f"to data/ and load it, so porting fidelity is diff-checkable. If an "
        f"edit to a vendored prompt is required, transform the verbatim original "
        f"in code and pin the result with a diff test."
    )


@pytest.mark.skipif(not PORTS, reason="no ported benchmarks yet")
@pytest.mark.parametrize("port", PORTS, ids=_IDS)
def test_port_routes_through_inspect_patches(port: Path):
    """Ports must import eval_mcp.inspect_patches so max_tokens is omitted.

    Otherwise Inspect's Bedrock provider injects a constant 2048 and reasoning
    models return empty completions that score 0 — see CLAUDE.md.
    """
    src = (port / "task.py").read_text(encoding="utf-8")
    assert "eval_mcp.inspect_patches" in src, (
        f"{port.name}/task.py must import eval_mcp.inspect_patches"
    )


@pytest.mark.skipif(not PORTS, reason="no ported benchmarks yet")
@pytest.mark.parametrize("port", PORTS, ids=_IDS)
def test_port_records_the_judge_in_score_metadata(port: Path):
    """If a port is judge-scored, every score must name its judge.

    The judge is part of the measuring instrument — swapping it can move the
    headline metric by several points — so a score that doesn't name its judge
    isn't interpretable.
    """
    src = (port / "task.py").read_text(encoding="utf-8")
    if "judge" not in src.lower():
        pytest.skip(f"{port.name} is not judge-scored")
    assert '"judge_model"' in src or "'judge_model'" in src, (
        f"{port.name}: judge-scored ports must record judge_model in the score "
        f"metadata so results are self-describing."
    )
