"""Unit tests for synthetic-RAG QA generation (attachSourceContext).

Covers the deterministic pieces only — the context-stamping helper and PDF
text extraction. The end-to-end flow (generate_qa_pairs → save_dataset →
create_eval_config RAG scoring) needs Bedrock + a subprocess and is exercised
by running the MCP, not by pytest (see CLAUDE.md).
"""

from __future__ import annotations

import pytest

from eval_mcp.tools.generate_qa import _attach_context
from eval_mcp.core.document_chunking import (
    PDFChunk,
    pdf_to_text,
    pdf_chunk_to_text,
)


# ---------------------------------------------------------------------------
# _attach_context — stamps retrieval_context onto each pair
# ---------------------------------------------------------------------------


def test_attach_context_stamps_each_pair():
    pairs = [
        {"question": "q1", "golden_answer": "a1"},
        {"question": "q2", "golden_answer": "a2"},
    ]
    _attach_context(pairs, "the source chunk")
    for p in pairs:
        assert p["retrieval_context"] == ["the source chunk"]


def test_attach_context_is_a_list_of_one():
    """RAG scorers expect retrieval_context: list[str]. One source chunk → [chunk]."""
    pairs = [{"question": "q", "golden_answer": "a"}]
    _attach_context(pairs, "chunk text")
    rc = pairs[0]["retrieval_context"]
    assert isinstance(rc, list) and len(rc) == 1 and isinstance(rc[0], str)


def test_attach_context_noop_on_empty_text():
    """An image / blank PDF page yields no text → leave the pair uncontextualised."""
    pairs = [{"question": "q", "golden_answer": "a"}]
    _attach_context(pairs, "")
    assert "retrieval_context" not in pairs[0]


def test_attach_context_noop_on_whitespace_only():
    pairs = [{"question": "q", "golden_answer": "a"}]
    _attach_context(pairs, "   \n\t  ")
    assert "retrieval_context" not in pairs[0]


def test_attach_context_returns_same_list():
    pairs = [{"question": "q", "golden_answer": "a"}]
    assert _attach_context(pairs, "x") is pairs


# ---------------------------------------------------------------------------
# PDF text extraction — pdf_to_text / pdf_chunk_to_text
# ---------------------------------------------------------------------------


def _make_pdf(page_texts: list[str]) -> bytes:
    """Build a minimal multi-page PDF with one known line of text per page."""
    fpdf = pytest.importorskip("fpdf")
    doc = fpdf.FPDF()
    doc.set_font("helvetica", size=12)
    for text in page_texts:
        doc.add_page()
        doc.cell(0, 10, text)
    out = doc.output()  # fpdf2 returns a bytearray
    return bytes(out)


def test_pdf_to_text_extracts_all_pages():
    pdf = _make_pdf(["AlphaPageOne", "BetaPageTwo", "GammaPageThree"])
    text = pdf_to_text(pdf)
    assert "AlphaPageOne" in text
    assert "BetaPageTwo" in text
    assert "GammaPageThree" in text


def test_pdf_to_text_skip_pages_drops_leading_pages():
    """skip_pages drops a chunk's context-overlap pages from the front."""
    pdf = _make_pdf(["OverlapContext", "RealContentHere"])
    text = pdf_to_text(pdf, skip_pages=1)
    assert "OverlapContext" not in text
    assert "RealContentHere" in text


def test_pdf_chunk_to_text_excludes_context_pages():
    """A PDFChunk's leading context_page_count pages are overlap — exclude them."""
    pdf = _make_pdf(["PrevChunkOverlap", "ThisChunkContent"])
    chunk = PDFChunk(
        pdf_bytes=pdf,
        context_page_count=1,  # first page is overlap from the previous chunk
        content_page_count=1,
        chunk_start_page=1,
        chunk_end_page=2,
    )
    text = pdf_chunk_to_text(chunk)
    assert "PrevChunkOverlap" not in text
    assert "ThisChunkContent" in text


def test_pdf_chunk_to_text_first_chunk_keeps_all():
    """First chunk has no overlap (context_page_count=0) — keep everything."""
    pdf = _make_pdf(["FirstChunkPageA", "FirstChunkPageB"])
    chunk = PDFChunk(
        pdf_bytes=pdf,
        context_page_count=0,
        content_page_count=2,
        chunk_start_page=1,
        chunk_end_page=2,
    )
    text = pdf_chunk_to_text(chunk)
    assert "FirstChunkPageA" in text
    assert "FirstChunkPageB" in text


def test_pdf_to_text_empty_on_garbage():
    """Non-PDF bytes shouldn't crash callers — surface as a read error, not a hang."""
    with pytest.raises(Exception):
        pdf_to_text(b"not a pdf at all")
