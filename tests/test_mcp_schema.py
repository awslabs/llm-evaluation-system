"""Schema-level tests for the eval-mcp server.

These tests don't exercise Bedrock or storage — they only check that the
MCP layer is wired correctly: every registered tool advertises annotations,
read-only tools say so, list tools accept pagination + response_format
parameters, and pagination metadata is returned in the expected shape.

Run with: uv run pytest tests/test_mcp_schema.py
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from eval_mcp import server as srv


# ---------------------------------------------------------------------------
# Tool annotations are present on every tool
# ---------------------------------------------------------------------------


def _registered_tools():
    return srv.mcp._tool_manager.list_tools()


def test_every_tool_has_annotations():
    """All MCP tools must advertise annotations so clients can decide
    whether a call is safe to auto-invoke."""
    missing = [t.name for t in _registered_tools() if t.annotations is None]
    assert missing == [], f"Tools without annotations: {missing}"


@pytest.mark.parametrize(
    "tool_name",
    [
        "list_datasets",
        "list_judges",
        "list_evaluations",
        "list_documents",
        "list_bedrock_models",
        "list_available_models",
        "get_evaluation_details",
        "analyze_dataset",
    ],
)
def test_read_only_tools_claim_read_only(tool_name: str):
    """Tools that don't mutate state should set readOnlyHint=True so
    clients can route them through cheap-path auto-approve."""
    tool = srv.mcp._tool_manager.get_tool(tool_name)
    assert tool is not None, f"Tool {tool_name} not registered"
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True, (
        f"{tool_name} should declare readOnlyHint=True"
    )
    assert tool.annotations.destructiveHint is False


@pytest.mark.parametrize(
    "tool_name",
    [
        "save_dataset",
        "generate_qa_pairs",
        "generate_judge",
        "create_eval_config",
        "run_evaluation",
        "run_evaluation_and_report",
        "retry_evaluation",
    ],
)
def test_write_tools_are_not_read_only(tool_name: str):
    tool = srv.mcp._tool_manager.get_tool(tool_name)
    assert tool is not None, f"Tool {tool_name} not registered"
    assert tool.annotations.readOnlyHint is False


def test_no_tool_declares_destructive_hint():
    """The eval server doesn't have any delete/drop semantics today. If
    that changes, update the corresponding tool's annotation explicitly
    rather than relying on the default."""
    destructive = [
        t.name for t in _registered_tools() if t.annotations and t.annotations.destructiveHint
    ]
    assert destructive == [], f"Unexpected destructive tools: {destructive}"


# ---------------------------------------------------------------------------
# Pagination params are exposed on the right tools
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    ["list_datasets", "list_judges", "list_evaluations", "list_documents"],
)
def test_list_tools_expose_pagination_params(tool_name: str):
    """Every list_* tool must accept limit, offset, and response_format
    so callers can page large result sets and pick markdown vs json."""
    tool = srv.mcp._tool_manager.get_tool(tool_name)
    assert tool is not None
    schema = tool.parameters  # JSON schema dict from FastMCP
    props = schema.get("properties", {})
    for required_param in ("limit", "offset", "response_format"):
        assert required_param in props, (
            f"{tool_name} is missing pagination param `{required_param}`. "
            f"Has: {sorted(props.keys())}"
        )


# ---------------------------------------------------------------------------
# Pagination behavior on list_datasets (handler-level, no DB needed)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_datasets(monkeypatch):
    """Substitute a fake DB result so we can drive the handler directly."""
    fake = [
        {"id": f"id-{i:02d}", "name": f"dataset-{i:02d}", "tests": [{"vars": {"question": f"q{i}"}}]}
        for i in range(25)
    ]

    from eval_mcp.tools import list_datasets as ld

    monkeypatch.setattr(ld, "list_datasets_from_db", lambda user_id, search_term: list(fake))
    return fake


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_list_datasets_json_pagination_shape(fake_datasets):
    """First page should report total/has_more/next_offset correctly."""
    from eval_mcp.tools.list_datasets import handle_list_datasets

    result = _run(
        handle_list_datasets(
            {"user_id": "u1", "limit": 10, "offset": 0, "response_format": "json"}
        )
    )
    payload = json.loads(result[0].text)
    assert payload["success"] is True
    assert payload["total"] == 25
    assert payload["count"] == 10
    assert payload["offset"] == 0
    assert payload["has_more"] is True
    assert payload["next_offset"] == 10
    assert len(payload["items"]) == 10
    assert payload["items"][0]["name"] == "dataset-00"


def test_list_datasets_last_page_marks_no_more(fake_datasets):
    """The final page sets has_more=False and next_offset=None."""
    from eval_mcp.tools.list_datasets import handle_list_datasets

    result = _run(
        handle_list_datasets(
            {"user_id": "u1", "limit": 10, "offset": 20, "response_format": "json"}
        )
    )
    payload = json.loads(result[0].text)
    assert payload["count"] == 5
    assert payload["has_more"] is False
    assert payload["next_offset"] is None


def test_list_datasets_markdown_shows_range_and_hint(fake_datasets):
    """Markdown output advertises the visible range and tells the agent
    how to fetch the next page when one exists."""
    from eval_mcp.tools.list_datasets import handle_list_datasets

    result = _run(
        handle_list_datasets(
            {"user_id": "u1", "limit": 5, "offset": 0, "response_format": "markdown"}
        )
    )
    text = result[0].text
    assert "Found 25 dataset(s)" in text
    assert "showing 1-5" in text
    assert "offset=5" in text  # next-page hint


def test_list_datasets_rejects_missing_user_id():
    """Defensive check — user_id gating shouldn't break with pagination args."""
    from eval_mcp.tools.list_datasets import handle_list_datasets

    result = _run(
        handle_list_datasets(
            {"limit": 10, "offset": 0, "response_format": "json"}
        )
    )
    payload = json.loads(result[0].text)
    assert payload["success"] is False
    assert "user_id" in payload["error"]
