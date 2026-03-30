"""Document chunking utilities for handling large files.

Supports intelligent chunking for both text files and PDFs to stay within
model context limits while preserving context continuity between chunks.

Text files: Token-based chunking with natural break detection
PDFs: Page-based chunking with overlap
"""

import io
from dataclasses import dataclass
from typing import List, Optional, Tuple

# Constants
MAX_CONTEXT_TOKENS = 100_000  # Threshold to trigger chunking
MAIN_CHUNK_TOKENS = 80_000  # Target tokens for main content per chunk
OVERLAP_TOKENS = 20_000  # Tokens from previous chunk as context
CHARS_PER_TOKEN = 4  # Approximate characters per token

# PDF constants - conservative to stay within model context limits
TOKENS_PER_PAGE_ESTIMATE = 3000  # Each page = text (1500-3000) + image tokens (per Anthropic docs)
MAIN_CHUNK_PAGES = 25  # Target pages for main content
OVERLAP_PAGES = 10  # Pages from previous chunk as context (reduced from 20)


@dataclass
class TextChunk:
    """A chunk of text content with optional context from previous chunk."""

    context: Optional[str]  # Content from previous chunk (do not generate QA from this)
    content: str  # Main content to generate QA from


@dataclass
class PDFChunk:
    """A chunk of PDF with extracted pages (max 100 pages for Bedrock limit)."""

    pdf_bytes: bytes  # Extracted PDF with context + content pages (≤100 total)
    context_page_count: int  # Number of context pages at start (0 if first chunk)
    content_page_count: int  # Number of content pages
    chunk_start_page: int  # Original PDF page number where this chunk starts
    chunk_end_page: int  # Original PDF page number where this chunk ends


def estimate_tokens_text(text: str) -> int:
    """Estimate token count for text content."""
    return len(text) // CHARS_PER_TOKEN


def estimate_tokens_pdf(pdf_bytes: bytes) -> Tuple[int, int]:
    """Estimate token count for PDF and return (estimated_tokens, page_count).

    Returns:
        Tuple of (estimated_tokens, page_count)
    """
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf_bytes))
        page_count = len(reader.pages)
        estimated_tokens = page_count * TOKENS_PER_PAGE_ESTIMATE
        return estimated_tokens, page_count
    except Exception:
        # If we can't read the PDF, assume it needs chunking to be safe
        # Estimate based on file size (~1 token per 2 bytes for PDFs)
        return len(pdf_bytes) // 2, 0


def needs_chunking(content_bytes: bytes, media_type: str) -> bool:
    """Check if document needs to be chunked based on estimated size.

    Args:
        content_bytes: Raw document bytes
        media_type: MIME type of the document

    Returns:
        True if document exceeds MAX_CONTEXT_TOKENS and needs chunking
    """
    if media_type == "application/pdf":
        estimated_tokens, _ = estimate_tokens_pdf(content_bytes)
    else:
        # Text-based content
        text = content_bytes.decode("utf-8", errors="replace")
        estimated_tokens = estimate_tokens_text(text)

    return estimated_tokens > MAX_CONTEXT_TOKENS


def find_natural_break(text: str, target_pos: int, max_pos: int) -> int:
    """Find a natural break point (section/paragraph boundary) between target and max.

    Looks for breaks in priority order:
    1. Section headers (markdown ## or similar)
    2. Triple newlines
    3. Double newlines (paragraph breaks)

    Args:
        text: The text to search in
        target_pos: Preferred position to start looking
        max_pos: Maximum position (hard limit)

    Returns:
        Position of the best break point, or max_pos if none found
    """
    # Priority order for break patterns
    break_patterns = [
        "\n## ",  # Markdown h2
        "\n# ",  # Markdown h1
        "\nCHAPTER ",  # Book chapters
        "\n\n\n",  # Triple newline
        "\n\n",  # Paragraph break
    ]

    for pattern in break_patterns:
        # Search forward from target to max
        pos = text.find(pattern, target_pos, max_pos)
        if pos > target_pos:
            return pos

    # No natural break found, fall back to max_pos
    # But try to at least break at a newline
    pos = text.rfind("\n", target_pos, max_pos)
    if pos > target_pos:
        return pos

    return max_pos


def chunk_text(text: str) -> List[TextChunk]:
    """Chunk text content with overlap for context continuity.

    Creates chunks of ~80k tokens with 20k token overlap from the previous chunk.
    Attempts to break at natural boundaries (paragraphs, sections).

    Args:
        text: The full text content

    Returns:
        List of TextChunk objects with context and content
    """
    estimated_tokens = estimate_tokens_text(text)

    # No chunking needed
    if estimated_tokens <= MAX_CONTEXT_TOKENS:
        return [TextChunk(context=None, content=text)]

    main_chunk_chars = MAIN_CHUNK_TOKENS * CHARS_PER_TOKEN
    overlap_chars = OVERLAP_TOKENS * CHARS_PER_TOKEN
    target_chars = main_chunk_chars  # 80k tokens
    max_chars = (MAIN_CHUNK_TOKENS + 10_000) * CHARS_PER_TOKEN  # 90k tokens max

    chunks = []
    pos = 0
    prev_chunk_end = 0

    while pos < len(text):
        # Calculate target and max end positions
        target_end = pos + target_chars
        max_end = min(pos + max_chars, len(text))

        # Find natural break point
        if max_end < len(text):
            end = find_natural_break(text, target_end, max_end)
        else:
            end = len(text)

        # Get context from previous chunk (if not first chunk)
        if chunks:
            # Take last overlap_chars from previous chunk's content
            context_start = max(0, prev_chunk_end - overlap_chars)
            # Try to start context at a paragraph boundary
            newline_pos = text.find("\n", context_start, prev_chunk_end)
            if newline_pos > context_start:
                context_start = newline_pos + 1
            context = text[context_start:prev_chunk_end]
        else:
            context = None

        # Extract main content
        content = text[pos:end]
        chunks.append(TextChunk(context=context, content=content))

        prev_chunk_end = end
        pos = end

    return chunks


def extract_pdf_pages(pdf_bytes: bytes, start_page: int, end_page: int) -> bytes:
    """Extract a range of pages from a PDF into a new PDF.

    Args:
        pdf_bytes: Original PDF bytes
        start_page: First page to extract (1-indexed)
        end_page: Last page to extract (1-indexed, inclusive)

    Returns:
        New PDF bytes containing only the specified pages
    """
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()

    # Convert to 0-indexed for pypdf
    for page_num in range(start_page - 1, end_page):
        if page_num < len(reader.pages):
            writer.add_page(reader.pages[page_num])

    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def chunk_pdf(pdf_bytes: bytes) -> List[PDFChunk]:
    """Chunk PDF into smaller PDFs with overlap for context continuity.

    Creates chunks of ~80 pages content + ~20 pages context (≤100 total for Bedrock limit).
    Each chunk is an actual extracted PDF, not just page range instructions.

    Args:
        pdf_bytes: Raw PDF bytes

    Returns:
        List of PDFChunk objects with extracted PDF bytes
    """
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf_bytes))
        total_pages = len(reader.pages)
    except Exception as e:
        raise ValueError(f"Failed to read PDF: {e}")

    # No chunking needed if under 100 pages
    if total_pages <= MAIN_CHUNK_PAGES + OVERLAP_PAGES:
        return [
            PDFChunk(
                pdf_bytes=pdf_bytes,
                context_page_count=0,
                content_page_count=total_pages,
                chunk_start_page=1,
                chunk_end_page=total_pages,
            )
        ]

    chunks = []
    content_start = 1  # 1-indexed

    while content_start <= total_pages:
        # Calculate content end page
        content_end = min(content_start + MAIN_CHUNK_PAGES - 1, total_pages)
        content_page_count = content_end - content_start + 1

        # Calculate context (pages from before content_start)
        if content_start > 1:
            context_start = max(1, content_start - OVERLAP_PAGES)
            context_page_count = content_start - context_start
        else:
            context_start = content_start
            context_page_count = 0

        # Extract pages into new PDF
        chunk_pdf_bytes = extract_pdf_pages(pdf_bytes, context_start, content_end)

        chunks.append(
            PDFChunk(
                pdf_bytes=chunk_pdf_bytes,
                context_page_count=context_page_count,
                content_page_count=content_page_count,
                chunk_start_page=context_start,
                chunk_end_page=content_end,
            )
        )

        content_start = content_end + 1

    return chunks


def format_chunk_prompt_text(chunk: TextChunk) -> str:
    """Format a text chunk into a prompt with clear separation.

    Args:
        chunk: TextChunk with optional context and main content

    Returns:
        Formatted prompt string
    """
    if chunk.context:
        return f"""<previous_context>
The following is content from the previous section for continuity.
Do NOT generate QA pairs from this section - it is only for context.

{chunk.context}
</previous_context>

<current_section>
Generate QA pairs ONLY from the following section:

{chunk.content}
</current_section>"""
    else:
        return f"""<document>
{chunk.content}
</document>"""


def format_chunk_prompt_pdf(chunk: PDFChunk, chunk_index: int, total_chunks: int) -> str:
    """Format a PDF chunk into descriptive context.

    Args:
        chunk: PDFChunk with extracted PDF and page info
        chunk_index: 0-based index of this chunk
        total_chunks: Total number of chunks

    Returns:
        Formatted context string describing the chunk
    """
    if chunk.context_page_count > 0:
        return f"""This is chunk {chunk_index + 1} of {total_chunks} from the document (pages {chunk.chunk_start_page}-{chunk.chunk_end_page}). The first {chunk.context_page_count} pages are context from the previous section - do NOT generate QA pairs from these."""
    else:
        return f"""This is chunk {chunk_index + 1} of {total_chunks} from the document (pages {chunk.chunk_start_page}-{chunk.chunk_end_page})."""
