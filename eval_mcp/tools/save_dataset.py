"""Save QA dataset (CSV, JSON, JSONL) to the database."""

import csv
import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.types import TextContent
from eval_mcp.core.user_storage import save_dataset_to_db


def parse_content_to_rows(content: str, filename: str) -> List[Dict[str, Any]]:
    """Parse file content to list of row dicts based on file extension."""
    ext = filename.lower().split(".")[-1] if "." in filename else "csv"

    if ext == "json":
        data = json.loads(content)
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            # Look for array field
            for field in ["data", "items", "rows", "records", "questions", "examples", "dataset"]:
                if field in data and isinstance(data[field], list):
                    return data[field]
            # Try first list value
            for value in data.values():
                if isinstance(value, list):
                    return value
        return []

    elif ext in ("jsonl", "ndjson"):
        rows = []
        for line in content.strip().split("\n"):
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows

    else:  # CSV
        sample = content[:2048]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
            delimiter = dialect.delimiter
        except csv.Error:
            delimiter = ","
        reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
        return list(reader)


def _coerce_retrieval_context(raw: Any) -> Optional[List[str]]:
    """Normalize a retrieval_context cell into ``list[str]`` or None.

    Accepts:
      - already a ``list[str]`` (JSON datasets — common path)
      - a JSON-encoded string like ``'["chunk1", "chunk2"]'`` (CSV input)
      - any string with the legacy ``"chunk1 ||| chunk2"`` separator
        used by some retrievers
      - falsy / empty values → None (row is treated as non-RAG)

    Anything else (list of non-strings, dict, etc.) raises ``ValueError``
    so callers can return a clean error instead of silently dropping.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, list):
        if all(isinstance(c, str) for c in raw):
            return [c for c in raw if c.strip()]
        raise ValueError("retrieval_context list must contain only strings")
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        # JSON-encoded list (the only sensible CSV encoding).
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"retrieval_context looks like JSON but didn't parse: {e}"
                )
            return _coerce_retrieval_context(parsed)
        # Pipe-separator fallback for retrievers that export plain CSV
        # with chunks joined by `|||`. Unambiguous because real chunks
        # rarely contain that token.
        if "|||" in stripped:
            return [c.strip() for c in stripped.split("|||") if c.strip()]
        # Single chunk as a bare string.
        return [stripped]
    raise ValueError(f"retrieval_context must be a list or string, got {type(raw).__name__}")


def rows_to_test_cases(
    rows: List[Dict[str, Any]],
    question_col: str,
    answer_col: str,
    retrieval_context_col: Optional[str] = None,
    actual_output_col: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Convert rows to test case format.

    Returns:
        List of test cases with ``vars.question`` and ``vars.golden_answer``.
        Optional columns:
          - ``retrieval_context_col`` (RAG mode) — captured as
            ``vars.retrieval_context`` (list[str], in retriever rank
            order — order matters for contextual_precision).
          - ``actual_output_col`` (score-only mode) — captured as
            ``vars.actual_output`` — signals ``create_eval_config`` to
            run in score-only mode (no candidate model invoked; the
            static answer is scored directly).

    The two columns are independent and can both be set on the same
    dataset (e.g. score pre-generated RAG outputs end-to-end).
    """
    test_cases = []
    for row in rows:
        q_val = str(row.get(question_col, "")).strip()
        a_val = str(row.get(answer_col, "")).strip()

        if not (q_val and a_val):
            continue
        vars_dict: Dict[str, Any] = {
            "question": q_val,
            "golden_answer": a_val,
        }
        if retrieval_context_col:
            chunks = _coerce_retrieval_context(row.get(retrieval_context_col))
            if chunks:
                vars_dict["retrieval_context"] = chunks
        if actual_output_col:
            ao_raw = row.get(actual_output_col)
            ao_val = str(ao_raw).strip() if ao_raw is not None else ""
            if ao_val:
                vars_dict["actual_output"] = ao_val
        test_cases.append({"vars": vars_dict})

    return test_cases


def generate_dataset_name(base_name: str) -> str:
    """Generate a clean dataset name from the original filename."""
    safe_name = "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in base_name)
    safe_name = safe_name.strip('_').lower()
    return safe_name if safe_name else "dataset"


async def handle_save_dataset(args: Dict[str, Any]) -> List[TextContent]:
    """Handle save_dataset tool call.

    Accepts either `file_path` (preferred — tool reads from disk) or
    `file_content` (raw string, kept for callers that already have it in-memory).

    Args:
        args: Tool arguments containing:
            - file_path: Absolute path to the dataset file on disk (CSV/JSON/JSONL)
            - file_content: Raw content as string (fallback when no path available)
            - filename: Optional display name (inferred from file_path when omitted)
            - user_id: User ID for storage isolation
            - column_mapping: {question: col_name, golden_answer: col_name}

    Returns:
        Result with saved path
    """
    file_path = args.get("file_path")
    file_content = args.get("file_content", "")
    filename = args.get("filename")
    user_id = args.get("user_id")
    column_mapping = args.get("column_mapping", {})

    if file_path and not file_content:
        try:
            file_content = Path(file_path).read_text()
            if not filename:
                filename = Path(file_path).name
        except Exception as e:
            return [TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": f"Could not read file_path {file_path!r}: {e}",
                }),
            )]

    if not filename:
        filename = "dataset.csv"

    if not file_content:
        return [TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": "Provide either file_path or file_content",
            }),
        )]

    if not user_id:
        return [TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": "user_id is required",
            }),
        )]

    question_col = column_mapping.get("question")
    answer_col = column_mapping.get("golden_answer")
    retrieval_context_col = column_mapping.get("retrieval_context")
    actual_output_col = column_mapping.get("actual_output")

    if not question_col or not answer_col:
        return [TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": "column_mapping must include both 'question' and 'golden_answer' column names",
            }),
        )]

    try:
        # Parse content and convert to test case format
        rows = parse_content_to_rows(file_content, filename)
        test_cases = rows_to_test_cases(
            rows,
            question_col,
            answer_col,
            retrieval_context_col=retrieval_context_col,
            actual_output_col=actual_output_col,
        )

        if not test_cases:
            return [TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": "No valid rows found with both question and answer",
                }),
            )]

        # Generate dataset name from original filename
        base_name = Path(filename).stem
        dataset_name = generate_dataset_name(base_name)

        # Save to database
        dataset_id = save_dataset_to_db(
            user_id,
            dataset_name,
            test_cases,
            source={"kind": "imported", "origin": filename},
        )

        rag_rows = sum(1 for tc in test_cases if "retrieval_context" in tc.get("vars", {}))
        ao_rows = sum(1 for tc in test_cases if "actual_output" in tc.get("vars", {}))
        result_payload: Dict[str, Any] = {
            "success": True,
            "dataset_id": dataset_id,
            "name": dataset_name,
            "rows_saved": len(test_cases),
        }
        if retrieval_context_col:
            result_payload["retrieval_context_rows"] = rag_rows
        if actual_output_col:
            result_payload["actual_output_rows"] = ao_rows
        return [TextContent(type="text", text=json.dumps(result_payload))]

    except Exception as e:
        return [TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": f"Failed to save dataset: {str(e)}",
            }),
        )]
