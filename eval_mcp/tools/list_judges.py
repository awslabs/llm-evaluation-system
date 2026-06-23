"""List available LLM judges from the database."""

import json
from typing import Any, Dict, List

from mcp.types import TextContent

from eval_mcp.core.user_storage import list_judges_from_db, merge_shared_rows


def _judge_summary(judge: Dict[str, Any]) -> Dict[str, Any]:
    config = judge.get("config") or {}
    criteria = config.get("criteria") or []
    return {
        "id": judge.get("id"),
        "name": judge.get("name"),
        "domain": config.get("domain", "unknown"),
        "criteria_count": len(criteria),
        "criteria_names": [c.get("name", "") for c in criteria],
    }


async def handle_list_judges(args: Dict[str, Any]) -> List[TextContent]:
    """List LLM judges with pagination and optional JSON format.

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

        all_judges = list_judges_from_db(user_id, search_term)
        all_judges.extend(merge_shared_rows(
            user_id, args.get("shared_scopes"),
            lambda owner: list_judges_from_db(owner, search_term),
        ))
        total = len(all_judges)
        page = all_judges[offset : offset + limit]
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
                            "items": [_judge_summary(j) for j in page],
                        },
                        indent=2,
                    ),
                )
            ]

        if not all_judges:
            msg = (
                f"No judges found matching '{search_term}'"
                if search_term
                else "No judges found. Create your first judge with generate_judge."
            )
            return [TextContent(type="text", text=msg)]

        output = f"Found {total} judge(s) — showing {offset + 1}-{offset + len(page)}:\n\n"
        for judge in page:
            s = _judge_summary(judge)
            names = s["criteria_names"]
            criteria_preview = ", ".join(names[:3])
            if len(names) > 3:
                criteria_preview += f" (+{len(names) - 3} more)"
            output += f"⚖️  **{s['name']}**\n"
            output += f"   ID: {s['id']}\n"
            output += f"   Domain: {s['domain']}\n"
            output += f"   Criteria ({s['criteria_count']}): {criteria_preview}\n\n"
        if has_more:
            output += f"More available — pass offset={next_offset} to see the next page.\n"

        return [TextContent(type="text", text=output)]

    except Exception as e:
        return [
            TextContent(
                type="text",
                text=f"Error listing judges: {str(e)}",
            )
        ]
