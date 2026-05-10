"""List available evaluation configuration files."""

import json
from pathlib import Path
from typing import Any, Dict, List
from mcp.types import TextContent


async def handle_list_eval_configs(args: Dict[str, Any]) -> List[TextContent]:
    """Handle list_eval_configs tool call.

    Lists all evaluation task files in the user's configs/ directory.
    Returns details about each config.
    """
    try:
        search_term = (args.get("searchTerm") or "").lower()
        user_id = args.get("user_id")

        from eval_mcp.core.user_storage import get_user_dir
        if user_id:
            configs_dir = get_user_dir(user_id) / "configs"
        else:
            configs_dir = Path("configs")

        if not configs_dir.exists():
            return [
                TextContent(
                    type="text",
                    text="No configs directory found. Create your first config with create_eval_config.",
                )
            ]

        # Find all Python task files
        config_files = list(configs_dir.glob("*.py"))

        if not config_files:
            return [
                TextContent(
                    type="text",
                    text="No evaluation configs found.",
                )
            ]

        configs_info = []
        for config_file in sorted(config_files):
            config_name = config_file.stem

            if search_term and search_term not in config_name.lower():
                continue

            config_info = {
                "name": config_name,
                "type": "inspect_task",
            }
            configs_info.append(config_info)

        if not configs_info:
            return [
                TextContent(
                    type="text",
                    text=f"No configs found matching '{search_term}'" if search_term else "No valid configs found",
                )
            ]

        return [TextContent(type="text", text=json.dumps({
            "success": True,
            "configs": configs_info,
            "total": len(configs_info),
        }, indent=2))]

    except Exception as e:
        return [
            TextContent(
                type="text",
                text=f"Error listing configs: {str(e)}",
            )
        ]
