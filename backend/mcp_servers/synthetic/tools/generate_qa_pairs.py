"""Generate question-answer pairs for LLM-as-judge evaluation."""

import asyncio
import base64
import json
import os
from typing import Any, Dict, List, Optional, Tuple

import yaml
from mcp.types import TextContent

from backend.core.bedrock_client import BedrockClient
from backend.core.user_storage import (
    get_user_documents_dir,
    get_document_content,
    save_dataset_to_db,
    MAX_DOCUMENTS,
)
from backend.core.logging_utils import get_logger, log_event
from backend.core.document_chunking import (
    needs_chunking,
    chunk_text,
    chunk_pdf,
    format_chunk_prompt_text,
    format_chunk_prompt_pdf,
    MAX_CONTEXT_TOKENS,
)

# Maximum QA pairs per chunk
MAX_QA_PER_CHUNK = 20

# Structured logger for QA generation
logger = get_logger("mcp_tools.synthetic.qa")


# Tool schema for structured QA output - forces reliable JSON generation
QA_PAIRS_TOOL = {
    "name": "submit_qa_pairs",
    "description": "Submit the generated question-answer pairs. You MUST call this tool with your QA pairs.",
    "input_schema": {
        "type": "object",
        "properties": {
            "qa_pairs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "A realistic question a user might ask",
                        },
                        "golden_answer": {
                            "type": "string",
                            "description": "The comprehensive, accurate answer",
                        },
                    },
                    "required": ["question", "golden_answer"],
                },
                "description": "List of question-answer pairs",
            },
        },
        "required": ["qa_pairs"],
    },
}

# Tool schema for personas generation
PERSONAS_TOOL = {
    "name": "submit_personas",
    "description": "Submit the generated user personas. You MUST call this tool with your personas.",
    "input_schema": {
        "type": "object",
        "properties": {
            "personas": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of diverse user persona descriptions",
            },
        },
        "required": ["personas"],
    },
}

# Tool schema for agent analysis output
AGENT_ANALYSIS_TOOL = {
    "name": "submit_agent_analysis",
    "description": "Submit the agent analysis with QA pairs and entry point. You MUST call this tool.",
    "input_schema": {
        "type": "object",
        "properties": {
            "framework": {
                "type": "string",
                "enum": ["strands", "crewai", "langgraph", "unknown"],
                "description": "Detected agent framework",
            },
            "entry_function": {
                "type": "string",
                "description": "Name of the main entry function to call",
            },
            "entry_call_pattern": {
                "type": "string",
                "description": "Python code pattern to invoke the agent with 'prompt' variable",
            },
            "qa_pairs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "golden_answer": {"type": "string"},
                    },
                    "required": ["question", "golden_answer"],
                },
                "description": "Test cases based on the agent's capabilities",
            },
        },
        "required": ["framework", "entry_function", "entry_call_pattern", "qa_pairs"],
    },
}


def detect_agent_framework(content: str) -> str:
    """Detect agent framework from Python file imports."""
    content_lower = content.lower()
    if "from strands" in content_lower or "import strands" in content_lower:
        return "strands"
    if "from crewai" in content_lower or "import crewai" in content_lower:
        return "crewai"
    if "from langgraph" in content_lower or "import langgraph" in content_lower:
        return "langgraph"
    return "unknown"


async def analyze_agent_code(
    bedrock: BedrockClient,
    user_id: str,
    doc_path: str,
    content: str,
    num_pairs: int,
    prompt: Optional[str] = None,
    instructions: Optional[str] = None,
) -> Dict[str, Any]:
    """Analyze Python agent code and generate test cases."""
    detected_framework = detect_agent_framework(content)
    system_prompt = "You are an expert at analyzing Python AI agent code."
    prompt_context = f"\nAgent purpose: {prompt}" if prompt else ""
    instructions_text = f"\nAdditional instructions: {instructions}" if instructions else ""

    user_prompt = f"""Analyze this Python agent file and generate test cases for evaluation.
{prompt_context}{instructions_text}

<python_code>
{content}
</python_code>

Detected framework hint: {detected_framework}

Tasks:
1. Confirm or correct the framework detection (strands, crewai, langgraph, or unknown)
2. Identify the main entry function/object that accepts user input
3. Determine the exact Python code pattern to invoke the agent with a 'prompt' variable
4. Generate {num_pairs} realistic question-answer test cases based on what this agent can do

For entry_call_pattern examples:
- Strands: "agent(prompt)" or "agent.invoke(prompt)"
- CrewAI: "crew.kickoff(inputs={{'query': prompt}})"
- LangGraph: "app.invoke({{'messages': [('user', prompt)]}})"

Submit your analysis using the submit_agent_analysis tool."""

    messages = [{"role": "user", "content": user_prompt}]
    response = await asyncio.to_thread(
        bedrock.create_message,
        messages=messages,
        tools=[AGENT_ANALYSIS_TOOL],
        tool_choice={"type": "auto"},
        system=system_prompt,
        max_tokens=8192,
    )

    tool_uses = bedrock.extract_tool_uses(response)
    if tool_uses:
        analysis = tool_uses[0]["input"]
        log_event(logger, "info", "agent_analysis_completed",
                  user_id=user_id, document=doc_path,
                  framework=analysis.get("framework"),
                  qa_count=len(analysis.get("qa_pairs", [])))
        return analysis

    log_event(logger, "warning", "agent_analysis_tool_not_used",
              user_id=user_id, document=doc_path)
    return {"framework": detected_framework, "entry_function": "unknown", "entry_call_pattern": "", "qa_pairs": []}


def generate_agent_wrapper(
    user_id: str,
    original_filename: str,
    framework: str,
    entry_call_pattern: str,
) -> str:
    """Generate a promptfoo-compatible wrapper file for the agent."""
    from pathlib import Path
    docs_dir = get_user_documents_dir(user_id)
    stem = Path(original_filename).stem
    wrapper_filename = f"{stem}_wrapper.py"
    wrapper_path = docs_dir / wrapper_filename

    if "/" in original_filename:
        folder, filename = original_filename.rsplit("/", 1)
        module_name = filename.replace(".py", "")
        import_line = f"sys.path.insert(0, str(Path(__file__).parent / '{folder}'))"
    else:
        module_name = stem
        import_line = "sys.path.insert(0, str(Path(__file__).parent))"

    wrapper_code = f'''"""Promptfoo wrapper for {original_filename}. Auto-generated."""
import sys
from pathlib import Path

# Add agent directory to path
{import_line}

# Import everything from the agent module
from {module_name} import *

def call_api(prompt, options, context):
    """Promptfoo provider interface."""
    try:
        result = {entry_call_pattern}
        if isinstance(result, str):
            output = result
        elif isinstance(result, dict):
            output = result.get("output") or result.get("result") or result.get("response") or str(result)
        elif hasattr(result, "content"):
            output = result.content
        elif hasattr(result, "output"):
            output = result.output
        else:
            output = str(result)
        return {{"output": output}}
    except Exception as e:
        return {{"error": str(e)}}
'''
    wrapper_path.write_text(wrapper_code)
    log_event(logger, "info", "agent_wrapper_generated", user_id=user_id, wrapper_path=str(wrapper_path))
    return str(wrapper_path)


async def generate_qa_pairs_from_agent(
    bedrock: BedrockClient,
    user_id: str,
    doc_path: str,
    content_bytes: bytes,
    num_pairs: int,
    prompt: Optional[str] = None,
    instructions: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate QA pairs from a Python agent file.

    Returns: {"qa_pairs": [...], "wrapper_path": "...", "framework": "...", "config_path": "..."}
    """
    content = content_bytes.decode("utf-8", errors="replace")
    analysis = await analyze_agent_code(
        bedrock, user_id, doc_path, content, num_pairs, prompt, instructions
    )
    qa_pairs = analysis.get("qa_pairs", [])
    if not qa_pairs:
        return {"qa_pairs": [], "wrapper_path": None, "framework": None, "config_path": None}

    framework = analysis.get("framework", "unknown")
    wrapper_path = generate_agent_wrapper(
        user_id, doc_path, framework, analysis.get("entry_call_pattern", ""),
    )

    return {
        "qa_pairs": qa_pairs,
        "wrapper_path": wrapper_path,
        "framework": framework,
    }


async def generate_personas(bedrock: BedrockClient, prompt: str, num_personas: int) -> List[str]:
    """Generate diverse user personas for QA pair generation using tool-based output."""
    system_prompt = "You are a helpful assistant that generates diverse user personas."

    user_prompt = f"""Consider the following AI system purpose:

<Purpose>
{prompt}
</Purpose>

Generate up to {num_personas} diverse user personas that would interact with this system.
Think about different backgrounds, expertise levels, goals, and contexts.

Submit your personas using the submit_personas tool."""

    messages = [{"role": "user", "content": user_prompt}]

    response = await asyncio.to_thread(
        bedrock.create_message,
        messages=messages,
        tools=[PERSONAS_TOOL],
        tool_choice={"type": "auto"},
        system=system_prompt,
        max_tokens=2048,
    )

    # Extract structured output from tool use
    tool_uses = bedrock.extract_tool_uses(response)
    if tool_uses:
        personas = tool_uses[0]["input"].get("personas", [])
        log_event(logger, "info", "personas_generated", count=len(personas))
        return personas

    # Fallback if tool use didn't happen (shouldn't occur with tool_choice)
    log_event(logger, "warning", "personas_tool_not_used",
              bedrock_response=response,
              prompt_preview=prompt[:200] if prompt else None)
    return []


def validate_qa_pairs(qa_pairs: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """
    Validate QA pairs have required structure.

    Returns:
        (is_valid, error_message)
    """
    if not qa_pairs:
        return False, "No QA pairs generated"

    if not isinstance(qa_pairs, list):
        return False, f"Expected list of QA pairs, got {type(qa_pairs).__name__}"

    invalid_pairs = []
    for i, qa in enumerate(qa_pairs):
        if not isinstance(qa, dict):
            invalid_pairs.append(f"Pair {i}: not a dict")
            continue

        missing_keys = []
        if "question" not in qa:
            missing_keys.append("question")
        if "golden_answer" not in qa:
            missing_keys.append("golden_answer")

        if missing_keys:
            invalid_pairs.append(f"Pair {i}: missing {missing_keys}, has keys {list(qa.keys())}")

    if invalid_pairs:
        error_msg = f"{len(invalid_pairs)}/{len(qa_pairs)} pairs invalid:\n" + "\n".join(invalid_pairs[:3])
        if len(invalid_pairs) > 3:
            error_msg += f"\n... and {len(invalid_pairs) - 3} more"
        return False, error_msg

    return True, ""


async def generate_qa_pairs_for_persona(
    bedrock: BedrockClient,
    prompt: str,
    persona: str,
    num_pairs: int,
    instructions: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Generate QA pairs from a specific persona's perspective using tool-based output."""
    system_prompt = "You are a helpful assistant that generates high-quality question-answer pairs."
    instructions_text = f"\nAdditional instructions: {instructions}" if instructions else ""

    user_prompt = f"""Generate {num_pairs} question-answer pair(s) for the following AI system from this user persona's perspective:

<Purpose>
{prompt}
</Purpose>

<Persona>
{persona}
</Persona>
{instructions_text}

Requirements:
- Questions should be realistic and relevant to this persona
- Golden answers should be comprehensive, accurate, and detailed
- Answers represent the ideal/correct response
- Cover different aspects and scenarios

Submit your QA pairs using the submit_qa_pairs tool."""

    messages = [{"role": "user", "content": user_prompt}]

    response = await asyncio.to_thread(
        bedrock.create_message,
        messages=messages,
        tools=[QA_PAIRS_TOOL],
        tool_choice={"type": "auto"},
        system=system_prompt,
        max_tokens=32768,  # Generous limit for detailed QA pairs
    )

    # Extract structured output from tool use
    tool_uses = bedrock.extract_tool_uses(response)
    if tool_uses:
        qa_pairs = tool_uses[0]["input"].get("qa_pairs", [])
        # Validate structure
        is_valid, error_msg = validate_qa_pairs(qa_pairs)
        if is_valid:
            log_event(logger, "info", "qa_pairs_generated_persona",
                      count=len(qa_pairs), persona_preview=persona[:100])
            return qa_pairs
        else:
            log_event(logger, "warning", "qa_validation_failed_persona",
                      error=error_msg,
                      bedrock_response=response,
                      persona_preview=persona[:100])
            return []

    # Fallback if tool use didn't happen (shouldn't occur with tool_choice)
    log_event(logger, "warning", "qa_tool_not_used_persona",
              bedrock_response=response,
              persona_preview=persona[:100])
    return []


async def _generate_qa_from_content(
    bedrock: BedrockClient,
    user_id: str,
    doc_path: str,
    content: Any,  # Either text string, base64 doc content, or PDF page instructions
    media_type: str,
    num_pairs: int,
    prompt: Optional[str] = None,
    instructions: Optional[str] = None,
    chunk_info: Optional[str] = None,  # Additional context for chunked documents
) -> List[Dict[str, Any]]:
    """Generate QA pairs from document content (internal helper).

    Args:
        bedrock: Bedrock client
        user_id: User ID for logging
        doc_path: Document path for logging
        content: The content to analyze (text, base64 doc, or page range instructions)
        media_type: MIME type of the document
        num_pairs: Number of QA pairs to generate
        prompt: Optional context about the AI system/use case
        instructions: Optional additional instructions
        chunk_info: Optional chunk-specific instructions (for large documents)

    Returns:
        List of QA pair dicts
    """
    system_prompt = "You are a helpful assistant that generates high-quality question-answer pairs based on document content."

    # Build prompt
    prompt_text = f"for a {prompt} system" if prompt else "based on the document"
    instructions_text = f"\nAdditional instructions: {instructions}" if instructions else ""
    chunk_text_section = f"\n{chunk_info}" if chunk_info else ""

    user_prompt = f"""Analyze the provided document and generate {num_pairs} question-answer pairs {prompt_text}.
{instructions_text}{chunk_text_section}

Requirements:
- Questions must be UNDERSTANDABLE without having read the document - include sufficient context (full names, dates, locations, roles) so the question makes sense on its own
- Focus on actual content (events, people, concepts, facts) - NOT document structure (table of contents, chapters, sections)
- Golden answers should be comprehensive, accurate, and sourced from the document
- Cover different aspects and topics from the document
- Vary question types (factual, analytical, cause-effect, comparative, etc.)

Submit your QA pairs using the submit_qa_pairs tool."""

    # Build message content based on type
    if isinstance(content, dict):
        # Already formatted as document/image content block
        doc_content = content
    else:
        # Text content
        doc_content = {"type": "text", "text": content}

    messages = [{
        "role": "user",
        "content": [
            doc_content,
            {"type": "text", "text": user_prompt},
        ],
    }]

    response = await asyncio.to_thread(
        bedrock.create_message,
        messages=messages,
        tools=[QA_PAIRS_TOOL],
        tool_choice={"type": "auto"},
        system=system_prompt,
        max_tokens=32768,  # Generous limit for detailed QA pairs
    )

    # Extract structured output from tool use
    tool_uses = bedrock.extract_tool_uses(response)
    if tool_uses:
        qa_pairs = tool_uses[0]["input"].get("qa_pairs", [])
        is_valid, error_msg = validate_qa_pairs(qa_pairs)
        if is_valid:
            log_event(logger, "info", "qa_pairs_generated_document",
                      user_id=user_id, document=doc_path, count=len(qa_pairs))
            return qa_pairs
        else:
            log_event(logger, "warning", "qa_validation_failed_document",
                      user_id=user_id, document=doc_path,
                      error=error_msg,
                      bedrock_response=response)
            return []

    # Tool not used - log the full response to understand why
    log_event(logger, "warning", "qa_tool_not_used_document",
              user_id=user_id, document=doc_path,
              bedrock_response=response)
    return []


async def generate_qa_pairs_from_document(
    bedrock: BedrockClient,
    user_id: str,
    doc_path: str,
    num_pairs: int,
    prompt: Optional[str] = None,
    instructions: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Generate QA pairs from a single document using multimodal input.

    Handles large documents by chunking them and generating QA pairs from each chunk.
    - Text files: Chunked by tokens with context overlap
    - PDFs: Chunked by pages with overlap

    Args:
        bedrock: Bedrock client
        user_id: User ID for document lookup
        doc_path: Path to document relative to user's documents folder
        num_pairs: Number of QA pairs to generate (up to MAX_QA_PER_CHUNK per chunk)
        prompt: Optional context about the AI system/use case
        instructions: Optional additional instructions

    Returns:
        List of QA pair dicts with 'question' and 'golden_answer' keys
    """
    # Load document content
    content_bytes, media_type = get_document_content(user_id, doc_path)

    # Check if document needs chunking
    if needs_chunking(content_bytes, media_type):
        log_event(logger, "info", "document_chunking_started",
                  user_id=user_id, document=doc_path, media_type=media_type)

        if media_type == "application/pdf":
            # PDF: Chunk by pages
            return await _generate_qa_from_pdf_chunks(
                bedrock, user_id, doc_path, content_bytes, num_pairs, prompt, instructions
            )
        else:
            # Text-based: Chunk by tokens
            return await _generate_qa_from_text_chunks(
                bedrock, user_id, doc_path, content_bytes, num_pairs, prompt, instructions
            )

    # Document is small enough - process as single unit
    content_b64 = base64.standard_b64encode(content_bytes).decode("utf-8")

    if media_type == "application/pdf":
        doc_content = {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": content_b64,
            },
        }
    elif media_type.startswith("image/"):
        doc_content = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": content_b64,
            },
        }
    else:
        # Text-based: decode and include as text
        text_content = content_bytes.decode("utf-8", errors="replace")
        doc_content = f"<document filename=\"{doc_path}\">\n{text_content}\n</document>"

    return await _generate_qa_from_content(
        bedrock, user_id, doc_path, doc_content, media_type,
        min(num_pairs, MAX_QA_PER_CHUNK), prompt, instructions
    )


async def _generate_qa_from_text_chunks(
    bedrock: BedrockClient,
    user_id: str,
    doc_path: str,
    content_bytes: bytes,
    num_pairs: int,
    prompt: Optional[str] = None,
    instructions: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Generate QA pairs from a large text document by chunking.

    Args:
        bedrock: Bedrock client
        user_id: User ID for logging
        doc_path: Document path
        content_bytes: Raw document bytes
        num_pairs: Requested number of QA pairs
        prompt: Optional context
        instructions: Optional additional instructions

    Returns:
        Combined list of QA pairs from all chunks
    """
    text = content_bytes.decode("utf-8", errors="replace")
    chunks = chunk_text(text)

    log_event(logger, "info", "text_chunks_created",
              user_id=user_id, document=doc_path, chunk_count=len(chunks))

    # Distribute requested pairs across chunks with remainder distribution
    base_pairs = max(1, min(MAX_QA_PER_CHUNK, num_pairs // len(chunks)))
    remainder = num_pairs % len(chunks)

    all_qa_pairs = []

    for i, chunk in enumerate(chunks):
        # First 'remainder' chunks get one extra pair
        pairs_for_this_chunk = base_pairs + (1 if i < remainder else 0)
        pairs_for_this_chunk = min(pairs_for_this_chunk, MAX_QA_PER_CHUNK)

        # Format chunk with context separation
        formatted_content = format_chunk_prompt_text(chunk)

        chunk_qa = await _generate_qa_from_content(
            bedrock, user_id, doc_path,
            formatted_content, "text/plain",
            pairs_for_this_chunk, prompt, instructions,
            chunk_info=f"This is chunk {i + 1} of {len(chunks)} from the document."
        )

        all_qa_pairs.extend(chunk_qa)

        log_event(logger, "info", "chunk_processed",
                  user_id=user_id, document=doc_path,
                  chunk=i + 1, total_chunks=len(chunks),
                  qa_count=len(chunk_qa))

    return all_qa_pairs


async def _generate_qa_from_pdf_chunks(
    bedrock: BedrockClient,
    user_id: str,
    doc_path: str,
    content_bytes: bytes,
    num_pairs: int,
    prompt: Optional[str] = None,
    instructions: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Generate QA pairs from a large PDF by processing page ranges.

    Sends the full PDF but instructs Claude to focus on specific page ranges.

    Args:
        bedrock: Bedrock client
        user_id: User ID for logging
        doc_path: Document path
        content_bytes: Raw PDF bytes
        num_pairs: Requested number of QA pairs
        prompt: Optional context
        instructions: Optional additional instructions

    Returns:
        Combined list of QA pairs from all page ranges
    """
    chunks = chunk_pdf(content_bytes)

    log_event(logger, "info", "pdf_chunks_created",
              user_id=user_id, document=doc_path, chunk_count=len(chunks))

    # Distribute requested pairs across chunks with remainder distribution
    base_pairs = max(1, min(MAX_QA_PER_CHUNK, num_pairs // len(chunks)))
    remainder = num_pairs % len(chunks)

    all_qa_pairs = []

    for i, chunk in enumerate(chunks):
        # First 'remainder' chunks get one extra pair
        pairs_for_this_chunk = base_pairs + (1 if i < remainder else 0)
        pairs_for_this_chunk = min(pairs_for_this_chunk, MAX_QA_PER_CHUNK)

        # Encode this chunk's extracted PDF
        chunk_b64 = base64.standard_b64encode(chunk.pdf_bytes).decode("utf-8")
        doc_content = {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": chunk_b64,
            },
        }

        # Format chunk context (informational, not instructional)
        page_instructions = format_chunk_prompt_pdf(chunk, i, len(chunks))

        chunk_qa = await _generate_qa_from_content(
            bedrock, user_id, doc_path,
            doc_content, "application/pdf",
            pairs_for_this_chunk, prompt, instructions,
            chunk_info=page_instructions
        )

        all_qa_pairs.extend(chunk_qa)

        log_event(logger, "info", "pdf_chunk_processed",
                  user_id=user_id, document=doc_path,
                  chunk=i + 1, total_chunks=len(chunks),
                  pages=f"{chunk.chunk_start_page}-{chunk.chunk_end_page}",
                  qa_count=len(chunk_qa))

    return all_qa_pairs


async def generate_qa_pairs_from_documents(
    bedrock: BedrockClient,
    user_id: str,
    doc_paths: List[str],
    num_samples: int,
    prompt: Optional[str] = None,
    instructions: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Generate QA pairs from multiple documents in parallel.

    Args:
        bedrock: Bedrock client
        user_id: User ID for document lookup
        doc_paths: List of document paths (max MAX_DOCUMENTS)
        num_samples: Total number of QA pairs to generate
        prompt: Optional context about the AI system
        instructions: Optional additional instructions

    Returns:
        List of all QA pairs from all documents
    """
    # Enforce limits
    if len(doc_paths) > MAX_DOCUMENTS:
        raise ValueError(f"Maximum {MAX_DOCUMENTS} documents allowed, got {len(doc_paths)}")

    # Distribute requested samples across documents
    # Large documents will be chunked and can generate more (20 per chunk)
    pairs_per_doc = max(1, num_samples // len(doc_paths))

    # Process documents in parallel
    tasks = [
        generate_qa_pairs_from_document(
            bedrock, user_id, doc_path, pairs_per_doc, prompt, instructions
        )
        for doc_path in doc_paths
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Collect results, handling any errors
    all_qa_pairs = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            log_event(logger, "error", "document_processing_error",
                      user_id=user_id, document=doc_paths[i],
                      error=str(result), error_type=type(result).__name__)
        else:
            all_qa_pairs.extend(result)

    # Return all generated pairs - chunked documents produce more QA pairs
    return all_qa_pairs


async def handle_generate_qa_pairs(bedrock: BedrockClient, args: Dict[str, Any]) -> List[TextContent]:
    """Handle generate_qa_pairs tool call.

    Supports two modes:
    1. Document-based: If 'documents' provided, generates QA from uploaded documents
    2. Persona-based: If no documents, generates synthetic QA from personas

    Args:
        bedrock: Bedrock client instance
        args: Tool arguments

    Returns:
        MCP TextContent response
    """
    prompt = args.get("prompt", "")
    instructions = args.get("instructions")
    num_samples = args.get("numSamples", 10)
    documents = args.get("documents", [])  # List of document paths
    user_id = args.get("user_id")

    # Validate user_id
    if not user_id:
        return [
            TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": "user_id is required",
                }),
            )
        ]

    # Determine mode based on documents presence
    use_documents = bool(documents)

    # Generate dataset name
    if use_documents:
        # Use first doc name for dataset naming
        first_doc = documents[0].replace("/", "_").replace(".", "_")
        safe_name = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in first_doc[:30])
    else:
        safe_name = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in prompt[:50])

    safe_name = safe_name.strip().replace(' ', '_').lower()

    # Track agent-specific outputs
    wrapper_path = None
    config_path = None

    try:
        if use_documents and documents[0].endswith('.py'):
            # Agent mode - Python agent file
            log_event(logger, "info", "qa_generation_started",
                      user_id=user_id, mode="agent",
                      document_count=len(documents), num_samples=num_samples)

            # For now, only support single agent file
            doc_path = documents[0]
            content_bytes, _ = get_document_content(user_id, doc_path)

            agent_result = await generate_qa_pairs_from_agent(
                bedrock, user_id, doc_path, content_bytes, num_samples, prompt, instructions
            )

            all_qa_pairs = agent_result["qa_pairs"]
            wrapper_path = agent_result["wrapper_path"]
            framework = agent_result["framework"]

            summary = {
                "totalGenerated": len(all_qa_pairs),
                "requested": num_samples,
                "mode": "agent",
                "framework": framework,
                "documents": documents,
            }

        elif use_documents:
            # Document-based generation
            # Large documents are chunked automatically (20 QA pairs per chunk)
            log_event(logger, "info", "qa_generation_started",
                      user_id=user_id, mode="document",
                      document_count=len(documents), num_samples=num_samples)

            all_qa_pairs = await generate_qa_pairs_from_documents(
                bedrock, user_id, documents, num_samples, prompt, instructions
            )

            summary = {
                "totalGenerated": len(all_qa_pairs),
                "requested": num_samples,
                "mode": "document",
                "documents": documents,
            }

        else:
            # Persona-based generation (original behavior)
            num_personas = args.get("numPersonas", 5)

            log_event(logger, "info", "qa_generation_started",
                      user_id=user_id, mode="persona",
                      num_personas=num_personas, num_samples=num_samples)

            # Validate prompt is provided for persona mode
            if not prompt:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps({
                            "success": False,
                            "error": "Either 'prompt' (for persona mode) or 'documents' (for document mode) is required",
                        }),
                    )
                ]

            personas = await generate_personas(bedrock, prompt, num_personas)

            if not personas:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps({
                            "success": False,
                            "error": "Failed to generate personas",
                        }),
                    )
                ]

            pairs_per_persona = max(1, num_samples // len(personas))
            all_qa_pairs = []

            for persona in personas:
                pairs = await generate_qa_pairs_for_persona(
                    bedrock, prompt, persona, pairs_per_persona, instructions
                )
                all_qa_pairs.extend(pairs)

            all_qa_pairs = all_qa_pairs[:num_samples]

            summary = {
                "totalGenerated": len(all_qa_pairs),
                "mode": "persona",
                "numPersonas": len(personas),
                "personas": personas,
            }

        # Check if we have any QA pairs at all
        if not all_qa_pairs:
            log_event(logger, "error", "qa_generation_failed",
                      user_id=user_id,
                      mode="document" if use_documents else "persona",
                      documents=documents if use_documents else None,
                      error="No QA pairs generated")
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "error": "No QA pairs generated",
                    }),
                )
            ]

        # Format as promptfoo test cases
        test_cases = []
        for qa in all_qa_pairs:
            if "question" in qa and "golden_answer" in qa:
                test_cases.append({
                    "vars": {
                        "question": qa["question"],
                        "golden_answer": qa["golden_answer"],
                    },
                })

        # Generate dataset name with actual count
        dataset_name = f"{safe_name}_{len(test_cases)}"

        # Save to database
        dataset_id = save_dataset_to_db(user_id, dataset_name, test_cases)

        # Log success
        log_event(logger, "info", "qa_generation_completed",
                  user_id=user_id,
                  mode="agent" if wrapper_path else ("document" if use_documents else "persona"),
                  count=len(test_cases),
                  dataset_name=dataset_name)

        # Return success response
        result = {
            "success": True,
            "dataset": dataset_name,
            "dataset_id": dataset_id,
            "summary": summary,
            "preview": test_cases[:3],
        }

        # Add agent-specific paths (use wrapperPath as provider in create_eval_config)
        if wrapper_path:
            result["wrapperPath"] = wrapper_path

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    except Exception as e:
        log_event(logger, "error", "qa_generation_exception",
                  user_id=user_id,
                  documents=documents if documents else None,
                  error=str(e),
                  error_type=type(e).__name__)
        return [
            TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": f"Failed to generate QA pairs: {str(e)}",
                }),
            )
        ]
