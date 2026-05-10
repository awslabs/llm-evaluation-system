"""Analyze QA dataset for structure and quality (CSV, JSON, JSONL)."""

import csv
import io
import json
from typing import Any, Dict, List, Optional, Tuple

from mcp.types import TextContent


# Common column/field name variations
QUESTION_ALIASES = {"question", "q", "input", "prompt", "query", "text", "user_input", "user"}
ANSWER_ALIASES = {"golden_answer", "answer", "a", "output", "expected", "response", "golden", "ideal", "target", "label"}


def parse_csv(content: str) -> Tuple[List[str], List[Dict[str, str]], str]:
    """Parse CSV content and return headers, rows, and any error.

    Returns:
        (headers, rows, error_message)
    """
    try:
        # Try to detect delimiter
        sample = content[:2048]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
        except csv.Error:
            dialect = csv.excel  # Default to comma-separated

        reader = csv.DictReader(io.StringIO(content), dialect=dialect)
        headers = reader.fieldnames or []

        if not headers:
            return [], [], "CSV has no headers"

        rows = list(reader)
        return headers, rows, ""

    except Exception as e:
        return [], [], f"CSV parsing error: {str(e)}"


def parse_json(content: str) -> Tuple[List[str], List[Dict[str, str]], str]:
    """Parse JSON content (array of objects) and return fields, rows, and any error."""
    try:
        data = json.loads(content)

        # Handle array at top level
        if isinstance(data, list):
            rows = data
        # Handle object with array field
        elif isinstance(data, dict):
            array_fields = ["data", "items", "rows", "records", "questions", "examples", "dataset"]
            rows = None
            for field in array_fields:
                if field in data and isinstance(data[field], list):
                    rows = data[field]
                    break
            if rows is None:
                for value in data.values():
                    if isinstance(value, list) and len(value) > 0:
                        rows = value
                        break
            if rows is None:
                return [], [], "JSON must contain an array of objects"
        else:
            return [], [], "JSON must be an array or object containing an array"

        if not rows:
            return [], [], "JSON array is empty"

        if not all(isinstance(row, dict) for row in rows):
            return [], [], "All items in JSON array must be objects"

        fields = list(rows[0].keys()) if rows else []
        string_rows = [{k: str(v) if v is not None else "" for k, v in row.items()} for row in rows]

        return fields, string_rows, ""

    except json.JSONDecodeError as e:
        return [], [], f"JSON parsing error: {str(e)}"
    except Exception as e:
        return [], [], f"JSON processing error: {str(e)}"


def parse_jsonl(content: str) -> Tuple[List[str], List[Dict[str, str]], str]:
    """Parse JSONL content (one JSON object per line) and return fields, rows, and any error."""
    try:
        rows = []
        fields = set()

        for line_num, line in enumerate(content.strip().split("\n"), 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    return [], [], f"Line {line_num}: Each line must be a JSON object"
                fields.update(obj.keys())
                string_row = {k: str(v) if v is not None else "" for k, v in obj.items()}
                rows.append(string_row)
            except json.JSONDecodeError as e:
                return [], [], f"Line {line_num}: Invalid JSON - {str(e)}"

        if not rows:
            return [], [], "JSONL file is empty"

        return list(fields), rows, ""

    except Exception as e:
        return [], [], f"JSONL processing error: {str(e)}"


def detect_column_mapping(headers: List[str]) -> Dict[str, Optional[str]]:
    """Detect which columns map to question and golden_answer.

    Returns:
        {"question": column_name or None, "golden_answer": column_name or None}
    """
    headers_lower = {h.lower().strip(): h for h in headers}

    question_col = None
    answer_col = None

    # Find question column
    for alias in QUESTION_ALIASES:
        if alias in headers_lower:
            question_col = headers_lower[alias]
            break

    # Find answer column
    for alias in ANSWER_ALIASES:
        if alias in headers_lower:
            answer_col = headers_lower[alias]
            break

    return {
        "question": question_col,
        "golden_answer": answer_col,
    }


def compute_stats(headers: List[str], rows: List[Dict[str, str]], mapping: Dict[str, Optional[str]]) -> Dict[str, Any]:
    """Compute statistics about the dataset."""
    stats = {
        "total_rows": len(rows),
        "columns": headers,
        "empty_cells": {},
        "usable_rows": 0,
    }

    # Count empty cells per column
    for col in headers:
        empty_count = sum(1 for row in rows if not row.get(col, "").strip())
        if empty_count > 0:
            stats["empty_cells"][col] = empty_count

    # Count usable rows (both question and answer present)
    if mapping["question"] and mapping["golden_answer"]:
        q_col = mapping["question"]
        a_col = mapping["golden_answer"]
        stats["usable_rows"] = sum(
            1 for row in rows
            if row.get(q_col, "").strip() and row.get(a_col, "").strip()
        )

    return stats


def sample_rows(
    rows: List[Dict[str, str]],
    mapping: Dict[str, Optional[str]],
    first_n: int = 5,
    last_n: int = 2,
) -> List[Dict[str, str]]:
    """Extract sample rows for preview, showing mapped columns."""
    samples = []

    # Get first N
    for row in rows[:first_n]:
        sample = {}
        if mapping["question"]:
            sample["question"] = row.get(mapping["question"], "")[:200]
        if mapping["golden_answer"]:
            sample["golden_answer"] = row.get(mapping["golden_answer"], "")[:200]
        samples.append(sample)

    # Get last N if there are more rows
    if len(rows) > first_n + last_n:
        samples.append({"_marker": f"... ({len(rows) - first_n - last_n} more rows) ..."})
        for row in rows[-last_n:]:
            sample = {}
            if mapping["question"]:
                sample["question"] = row.get(mapping["question"], "")[:200]
            if mapping["golden_answer"]:
                sample["golden_answer"] = row.get(mapping["golden_answer"], "")[:200]
            samples.append(sample)

    return samples


async def handle_analyze_dataset(args: Dict[str, Any]) -> List[TextContent]:
    """Handle analyze_dataset tool call.

    Accepts `file_path` (preferred) or `file_content` (raw string).

    Args:
        args: Tool arguments containing file_path or file_content, and optional filename

    Returns:
        Analysis report as TextContent
    """
    from pathlib import Path as _Path

    file_path = args.get("file_path")
    file_content = args.get("file_content", "")
    filename = args.get("filename")

    if file_path and not file_content:
        try:
            file_content = _Path(file_path).read_text()
            if not filename:
                filename = _Path(file_path).name
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

    # Detect format and parse
    ext = filename.lower().split(".")[-1] if "." in filename else "csv"

    if ext == "json":
        headers, rows, error = parse_json(file_content)
    elif ext in ("jsonl", "ndjson"):
        headers, rows, error = parse_jsonl(file_content)
    else:
        headers, rows, error = parse_csv(file_content)

    if error:
        return [TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": error,
            }),
        )]

    if not rows:
        return [TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": "File is empty (no data rows)",
            }),
        )]

    # Detect column mapping
    mapping = detect_column_mapping(headers)

    # Compute stats
    stats = compute_stats(headers, rows, mapping)

    # Build issues list
    issues = []
    if not mapping["question"]:
        issues.append(f"Could not detect question column. Available columns: {headers}. Expected one of: {sorted(QUESTION_ALIASES)}")
    if not mapping["golden_answer"]:
        issues.append(f"Could not detect answer column. Available columns: {headers}. Expected one of: {sorted(ANSWER_ALIASES)}")

    if mapping["question"] and mapping["golden_answer"]:
        q_empty = stats["empty_cells"].get(mapping["question"], 0)
        a_empty = stats["empty_cells"].get(mapping["golden_answer"], 0)
        if q_empty > 0:
            issues.append(f"{q_empty} rows have empty questions")
        if a_empty > 0:
            issues.append(f"{a_empty} rows have empty answers")

    # Get samples
    samples = sample_rows(rows, mapping)

    # Determine validity
    valid = bool(mapping["question"] and mapping["golden_answer"] and stats["usable_rows"] > 0)

    # Build response
    result = {
        "success": True,
        "format": ext,
        "valid": valid,
        "filename": filename,
        "column_mapping": mapping,
        "stats": stats,
        "issues": issues,
        "samples": samples,
        "fields": headers,  # All field names for Claude fallback
    }

    # Include raw sample for Claude fallback when auto-detect fails
    if not valid:
        # First 3 rows as raw data for LLM analysis
        result["raw_sample"] = rows[:3] if len(rows) >= 3 else rows

    return [TextContent(type="text", text=json.dumps(result, indent=2))]
