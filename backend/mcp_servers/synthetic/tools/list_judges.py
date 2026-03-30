"""List available LLM judges from the database."""

import json
from typing import Any, Dict, List

from mcp.types import TextContent

from backend.core.user_storage import list_judges_from_db


async def handle_list_judges(args: Dict[str, Any]) -> List[TextContent]:
    """Handle list_judges tool call.

    Lists all LLM judges from the user's database.
    Returns details about each judge including domain and criteria.
    """
    try:
        # Get required user_id and optional search filter
        user_id = args.get("user_id")
        search_term = (args.get("searchTerm") or "").lower()

        if not user_id:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"success": False, "error": "user_id is required"}),
                )
            ]

        # Get judges from database
        judges = list_judges_from_db(user_id, search_term)

        if not judges:
            msg = f"No judges found matching '{search_term}'" if search_term else "No judges found. Create your first judge with generate_judge."
            return [TextContent(type="text", text=msg)]

        # Format output
        output = f"Found {len(judges)} judge(s):\n\n"

        for judge in judges:
            config = judge["config"]
            domain = config.get("domain", "unknown")
            criteria = config.get("criteria", [])
            criteria_count = len(criteria)

            # Format criteria preview
            criteria_preview = ", ".join([c.get("name", "") for c in criteria[:3]])
            if len(criteria) > 3:
                criteria_preview += f" (+{len(criteria) - 3} more)"

            output += f"⚖️  **{judge['name']}**\n"
            output += f"   ID: {judge['id']}\n"
            output += f"   Domain: {domain}\n"
            output += f"   Criteria ({criteria_count}): {criteria_preview}\n\n"

        return [TextContent(type="text", text=output)]

    except Exception as e:
        return [
            TextContent(
                type="text",
                text=f"Error listing judges: {str(e)}",
            )
        ]
