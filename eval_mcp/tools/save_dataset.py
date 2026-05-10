"""Save QA dataset (CSV, JSON, JSONL) to the database."""

import csv
import io
import json
from pathlib import Path
from typing import Any, Dict, List

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


def rows_to_test_cases(
    rows: List[Dict[str, Any]],
    question_col: str,
    answer_col: str,
) -> List[Dict[str, Any]]:
    """Convert rows to test case format.

    Returns:
        List of test cases with vars.question and vars.golden_answer
    """
    test_cases = []
    for row in rows:
        q_val = str(row.get(question_col, "")).strip()
        a_val = str(row.get(answer_col, "")).strip()

        if q_val and a_val:
            test_cases.append({
                "vars": {
                    "question": q_val,
                    "golden_answer": a_val,
                }
            })

    return test_cases


def generate_dataset_name(base_name: str) -> str:
    """Generate a clean dataset name from the original filename."""
    safe_name = "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in base_name)
    safe_name = safe_name.strip('_').lower()
    return safe_name if safe_name else "dataset"


async def handle_save_dataset(args: Dict[str, Any]) -> List[TextContent]:
    """Handle save_dataset tool call.

    Args:
        args: Tool arguments containing:
            - file_content: Raw CSV content
            - filename: Original filename (used for naming)
            - user_id: User ID for storage isolation
            - column_mapping: {question: col_name, golden_answer: col_name}

    Returns:
        Result with saved path
    """
    file_content = args.get("file_content", "")
    filename = args.get("filename", "dataset.csv")
    user_id = args.get("user_id")
    column_mapping = args.get("column_mapping", {})

    if not file_content:
        return [TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": "No file content provided",
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
        test_cases = rows_to_test_cases(rows, question_col, answer_col)

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
        dataset_id = save_dataset_to_db(user_id, dataset_name, test_cases)

        return [TextContent(
            type="text",
            text=json.dumps({
                "success": True,
                "dataset_id": dataset_id,
                "name": dataset_name,
                "rows_saved": len(test_cases),
            }),
        )]

    except Exception as e:
        return [TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": f"Failed to save dataset: {str(e)}",
            }),
        )]
