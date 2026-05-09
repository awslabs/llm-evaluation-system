"""Dataset analysis agent with its own tools and agentic loop."""

import json
import logging
from typing import Any, Dict, List, Optional

from backend.core.bedrock_client import BedrockClient

# Set up logging
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


# Internal tools for the dataset agent
TOOLS = [
    {
        "name": "parse_csv",
        "description": "Parse CSV content and extract headers, row count, and detect delimiter. Returns structure info.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The raw CSV file content",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "get_sample_rows",
        "description": "Get sample rows from the parsed CSV for inspection. Call parse_csv first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The raw CSV file content",
                },
                "first_n": {
                    "type": "integer",
                    "description": "Number of first rows to get (default: 5)",
                    "default": 5,
                },
                "last_n": {
                    "type": "integer",
                    "description": "Number of last rows to get (default: 2)",
                    "default": 2,
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "check_column_mapping",
        "description": "Validate that specific columns exist and check for empty values.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The raw CSV file content",
                },
                "question_column": {
                    "type": "string",
                    "description": "The column name to use as 'question'",
                },
                "answer_column": {
                    "type": "string",
                    "description": "The column name to use as 'golden_answer'",
                },
            },
            "required": ["content", "question_column", "answer_column"],
        },
    },
    {
        "name": "submit_analysis",
        "description": "Submit your final analysis. Call this when you've completed your assessment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "valid": {
                    "type": "boolean",
                    "description": "Whether the dataset is valid and ready to use",
                },
                "column_mapping": {
                    "type": "object",
                    "description": "Mapping of detected columns: {question: 'col_name', golden_answer: 'col_name'}",
                    "properties": {
                        "question": {"type": ["string", "null"]},
                        "golden_answer": {"type": ["string", "null"]},
                    },
                },
                "issues": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of issues found (empty if none)",
                },
                "summary": {
                    "type": "string",
                    "description": "Brief summary of the dataset and its readiness",
                },
                "usable_rows": {
                    "type": "integer",
                    "description": "Number of rows that have both question and answer filled",
                },
            },
            "required": ["valid", "column_mapping", "issues", "summary", "usable_rows"],
        },
    },
]

SYSTEM_PROMPT = """You are a dataset validation specialist. Your job is to analyze CSV files that will be used for LLM evaluation.

The target format needs two key columns:
- **question**: The input/prompt to test the LLM with
- **golden_answer**: The ideal/expected answer to compare against

Your workflow:
1. First, call parse_csv to understand the structure
2. Call get_sample_rows to see actual data
3. Identify which columns map to question and golden_answer
4. Call check_column_mapping to validate your mapping
5. Call submit_analysis with your final assessment

Be flexible with column names - users may use variations like:
- For questions: q, input, prompt, query, text, user_input, user, question
- For answers: answer, a, output, expected, response, golden, ideal, target, golden_answer, label

If you can't find suitable columns, explain what's missing in your analysis.

Always complete your analysis by calling submit_analysis."""


class DatasetAgent:
    """Agent specialized for dataset analysis."""

    def __init__(self, bedrock_client: BedrockClient):
        self.bedrock = bedrock_client
        self.conversation_history: List[Dict[str, Any]] = []
        self._final_analysis: Optional[Dict[str, Any]] = None

    def _execute_tool(self, tool_name: str, args: Dict[str, Any]) -> str:
        """Execute an internal tool and return result."""

        if tool_name == "parse_csv":
            return self._tool_parse_csv(args.get("content", ""))

        elif tool_name == "get_sample_rows":
            return self._tool_get_sample_rows(
                args.get("content", ""),
                args.get("first_n", 5),
                args.get("last_n", 2),
            )

        elif tool_name == "check_column_mapping":
            return self._tool_check_column_mapping(
                args.get("content", ""),
                args.get("question_column", ""),
                args.get("answer_column", ""),
            )

        elif tool_name == "submit_analysis":
            self._final_analysis = {
                "valid": args.get("valid", False),
                "column_mapping": args.get("column_mapping", {}),
                "issues": args.get("issues", []),
                "summary": args.get("summary", ""),
                "usable_rows": args.get("usable_rows", 0),
            }
            return "Analysis submitted successfully."

        else:
            return f"Unknown tool: {tool_name}"

    def _tool_parse_csv(self, content: str) -> str:
        """Parse CSV and return structure info."""
        import csv
        import io

        try:
            # Detect delimiter
            sample = content[:2048]
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
                delimiter = dialect.delimiter
            except csv.Error:
                delimiter = ","

            reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
            headers = reader.fieldnames or []

            if not headers:
                return json.dumps({"error": "No headers found in CSV"})

            rows = list(reader)

            # Count empty cells per column
            empty_counts = {}
            for col in headers:
                empty_count = sum(1 for row in rows if not row.get(col, "").strip())
                if empty_count > 0:
                    empty_counts[col] = empty_count

            return json.dumps({
                "headers": headers,
                "row_count": len(rows),
                "delimiter": delimiter,
                "empty_cells_per_column": empty_counts,
            })

        except Exception as e:
            return json.dumps({"error": f"CSV parsing failed: {str(e)}"})

    def _tool_get_sample_rows(self, content: str, first_n: int, last_n: int) -> str:
        """Get sample rows from CSV."""
        import csv
        import io

        try:
            sample = content[:2048]
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
                delimiter = dialect.delimiter
            except csv.Error:
                delimiter = ","

            reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
            rows = list(reader)

            first_rows = rows[:first_n]
            last_rows = rows[-last_n:] if len(rows) > first_n + last_n else []

            # Truncate long values for readability
            def truncate_row(row):
                return {k: v[:150] + "..." if len(v) > 150 else v for k, v in row.items()}

            return json.dumps({
                "first_rows": [truncate_row(r) for r in first_rows],
                "last_rows": [truncate_row(r) for r in last_rows],
                "total_rows": len(rows),
            })

        except Exception as e:
            return json.dumps({"error": f"Failed to get samples: {str(e)}"})

    def _tool_check_column_mapping(self, content: str, question_col: str, answer_col: str) -> str:
        """Check if column mapping is valid."""
        import csv
        import io

        try:
            sample = content[:2048]
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
                delimiter = dialect.delimiter
            except csv.Error:
                delimiter = ","

            reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
            headers = reader.fieldnames or []
            rows = list(reader)

            issues = []

            # Check columns exist
            if question_col not in headers:
                issues.append(f"Column '{question_col}' not found. Available: {headers}")
            if answer_col not in headers:
                issues.append(f"Column '{answer_col}' not found. Available: {headers}")

            if issues:
                return json.dumps({"valid": False, "issues": issues})

            # Count usable rows
            usable = 0
            empty_questions = 0
            empty_answers = 0

            for row in rows:
                q_val = row.get(question_col, "").strip()
                a_val = row.get(answer_col, "").strip()

                if q_val and a_val:
                    usable += 1
                if not q_val:
                    empty_questions += 1
                if not a_val:
                    empty_answers += 1

            if empty_questions > 0:
                issues.append(f"{empty_questions} rows have empty questions")
            if empty_answers > 0:
                issues.append(f"{empty_answers} rows have empty answers")

            return json.dumps({
                "valid": usable > 0,
                "usable_rows": usable,
                "total_rows": len(rows),
                "empty_questions": empty_questions,
                "empty_answers": empty_answers,
                "issues": issues,
            })

        except Exception as e:
            return json.dumps({"error": f"Validation failed: {str(e)}"})

    async def analyze(self, file_content: str, filename: str) -> Dict[str, Any]:
        """
        Run the analysis agent loop.

        Args:
            file_content: Raw CSV content
            filename: Name of the file

        Returns:
            Analysis result dict
        """
        import asyncio

        logger.info(f"Starting analysis of '{filename}' ({len(file_content)} bytes)")

        # Basic validation - check if content looks like text
        try:
            # Check for binary content (non-printable characters)
            sample = file_content[:1000]
            non_printable = sum(1 for c in sample if ord(c) < 32 and c not in '\n\r\t')
            if non_printable > len(sample) * 0.1:  # More than 10% non-printable
                logger.error(f"File appears to be binary, not CSV ({non_printable} non-printable chars in first 1000)")
                return {
                    "valid": False,
                    "column_mapping": {"question": None, "golden_answer": None},
                    "issues": ["File appears to be binary or corrupted, not a valid CSV text file"],
                    "summary": "Invalid file format",
                    "usable_rows": 0,
                }
        except Exception as e:
            logger.error(f"Validation error: {e}")
            return {
                "valid": False,
                "column_mapping": {"question": None, "golden_answer": None},
                "issues": [f"File validation error: {str(e)}"],
                "summary": "Could not validate file",
                "usable_rows": 0,
            }

        # Initial message to the agent
        user_message = f"""Analyze this CSV dataset file: "{filename}"

Here is the file content:

```csv
{file_content[:10000]}
```

{"(File truncated - showing first 10000 characters)" if len(file_content) > 10000 else ""}

Please analyze this dataset and determine:
1. What columns are available
2. Which columns should map to "question" and "golden_answer"
3. Data quality issues (empty cells, etc.)
4. Whether this dataset is ready for use

Use your tools to analyze, then submit your final analysis."""

        self.conversation_history = [{"role": "user", "content": user_message}]
        self._final_analysis = None

        max_iterations = 10

        for iteration in range(max_iterations):
            logger.info(f"[{filename}] Iteration {iteration + 1}/{max_iterations}")

            try:
                response = await asyncio.to_thread(
                    self.bedrock.create_message,
                    messages=self.conversation_history,
                    tools=TOOLS,
                    system=SYSTEM_PROMPT,
                    max_tokens=4096,
                )
            except Exception as e:
                logger.error(f"[{filename}] Bedrock API error: {e}")
                return {
                    "valid": False,
                    "column_mapping": {"question": None, "golden_answer": None},
                    "issues": [f"LLM API error: {str(e)}"],
                    "summary": "Analysis failed due to API error",
                    "usable_rows": 0,
                }

            stop_reason = response.get("stop_reason")

            if stop_reason == "end_turn":
                logger.info(f"[{filename}] Done (no submission)")
                if self._final_analysis:
                    return self._final_analysis
                return {
                    "valid": False,
                    "column_mapping": {"question": None, "golden_answer": None},
                    "issues": ["Agent did not complete analysis"],
                    "summary": self.bedrock.extract_text_from_response(response),
                    "usable_rows": 0,
                }

            elif stop_reason == "tool_use":
                tool_uses = self.bedrock.extract_tool_uses(response)
                tool_names = [t['name'] for t in tool_uses]
                logger.info(f"[{filename}] Tools: {tool_names}")

                self.conversation_history.append({
                    "role": "assistant",
                    "content": response.get("content", []),
                })

                tool_results = []
                for tool_use in tool_uses:
                    result = self._execute_tool(tool_use["name"], tool_use["input"])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use["id"],
                        "content": result,
                    })

                    if tool_use["name"] == "submit_analysis" and self._final_analysis:
                        logger.info(f"[{filename}] Analysis complete: valid={self._final_analysis.get('valid')}")
                        return self._final_analysis

                self.conversation_history.append({"role": "user", "content": tool_results})

            else:
                logger.warning(f"[{filename}] Unexpected stop_reason: {stop_reason}")
                break

        # Max iterations or unexpected exit
        logger.warning(f"[{filename}] Max iterations reached")
        if self._final_analysis:
            return self._final_analysis

        return {
            "valid": False,
            "column_mapping": {"question": None, "golden_answer": None},
            "issues": ["Analysis did not complete within iteration limit"],
            "summary": "Analysis incomplete",
            "usable_rows": 0,
        }
