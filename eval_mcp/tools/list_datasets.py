"""List available datasets from the database."""

import json
from typing import Any, Dict, List

from mcp.types import TextContent

from eval_mcp.core.user_storage import list_datasets_from_db


async def handle_list_datasets(args: Dict[str, Any]) -> List[TextContent]:
    """Handle list_datasets tool call.

    Lists all datasets from the user's database.
    Returns details about each dataset including number of samples and preview.
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

        # Get datasets from database
        datasets = list_datasets_from_db(user_id, search_term)

        if not datasets:
            msg = f"No datasets found matching '{search_term}'" if search_term else "No datasets found. Create your first dataset with save_dataset."
            return [TextContent(type="text", text=msg)]

        # Format output
        output = f"Found {len(datasets)} dataset(s):\n\n"

        for dataset in datasets:
            tests = dataset.get("tests", [])
            num_samples = dataset.get("num_samples", len(tests) if isinstance(tests, list) else 0)

            # Get first question as preview
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

            output += f"📊 **{dataset['name']}**\n"
            output += f"   ID: {dataset['id'][:16]}...\n"
            output += f"   Samples: {num_samples}\n"
            output += f"   Preview: {preview or 'No preview available'}\n\n"

        return [TextContent(type="text", text=output)]

    except Exception as e:
        return [
            TextContent(
                type="text",
                text=f"Error listing datasets: {str(e)}",
            )
        ]
