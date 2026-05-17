"""List available datasets from the database."""

import json
from typing import Any, Dict, List

from mcp.types import TextContent

from eval_mcp.core.user_storage import list_datasets_from_db


def _dataset_preview(dataset: Dict[str, Any]) -> Dict[str, Any]:
    tests = dataset.get("tests", [])
    num_samples = dataset.get("num_samples", len(tests) if isinstance(tests, list) else 0)
    preview = ""
    if isinstance(tests, list) and len(tests) > 0:
        first_item = tests[0]
        if isinstance(first_item, dict):
            preview = (
                first_item.get("vars", {}).get("question")
                or first_item.get("question")
                or str(first_item)[:100]
            )
            preview = preview[:100]
            if len(preview) == 100:
                preview += "..."
    return {
        "id": dataset.get("id"),
        "name": dataset.get("name"),
        "num_samples": num_samples,
        "preview": preview or None,
    }


async def handle_list_datasets(args: Dict[str, Any]) -> List[TextContent]:
    """List datasets with pagination and optional JSON format.

    Args (from `args` dict):
        user_id: required
        searchTerm: optional case-insensitive name filter
        limit: page size, default 20
        offset: page start, default 0
        response_format: "markdown" (default) or "json"
    """
    try:
        user_id = args.get("user_id")
        search_term = (args.get("searchTerm") or "").lower()
        limit = max(1, int(args.get("limit", 20) or 20))
        offset = max(0, int(args.get("offset", 0) or 0))
        response_format = (args.get("response_format") or "markdown").lower()

        if not user_id:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"success": False, "error": "user_id is required"}),
                )
            ]

        all_datasets = list_datasets_from_db(user_id, search_term)
        total = len(all_datasets)
        page = all_datasets[offset : offset + limit]
        has_more = offset + len(page) < total
        next_offset = offset + len(page) if has_more else None

        if response_format == "json":
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "success": True,
                            "total": total,
                            "count": len(page),
                            "offset": offset,
                            "has_more": has_more,
                            "next_offset": next_offset,
                            "items": [_dataset_preview(d) for d in page],
                        },
                        indent=2,
                    ),
                )
            ]

        if not all_datasets:
            msg = (
                f"No datasets found matching '{search_term}'"
                if search_term
                else "No datasets found. Create your first dataset with save_dataset."
            )
            return [TextContent(type="text", text=msg)]

        output = f"Found {total} dataset(s) — showing {offset + 1}-{offset + len(page)}:\n\n"
        for dataset in page:
            p = _dataset_preview(dataset)
            output += f"📊 **{p['name']}**\n"
            output += f"   ID: {(p['id'] or '')[:16]}...\n"
            output += f"   Samples: {p['num_samples']}\n"
            output += f"   Preview: {p['preview'] or 'No preview available'}\n\n"
        if has_more:
            output += f"More available — pass offset={next_offset} to see the next page.\n"

        return [TextContent(type="text", text=output)]

    except Exception as e:
        return [
            TextContent(
                type="text",
                text=f"Error listing datasets: {str(e)}",
            )
        ]
