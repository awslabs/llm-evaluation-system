"""Fetch the full record for a single optimization run.

Returns everything the frontend needs to render the detail tab: history
of per-iteration prompts + train pass rates, per-iteration test scores,
winner selection, rationales, and the metadata that identifies the
underlying dataset / judge / providers.
"""

import json
from typing import Any, Dict, List

from mcp.types import TextContent

from eval_mcp.core.user_storage import get_optimization_from_db


async def handle_get_optimization_details(args: Dict[str, Any]) -> List[TextContent]:
    """Args (from ``args`` dict):
        user_id: required
        optimization_id: required
    """
    try:
        user_id = args.get("user_id")
        optimization_id = args.get("optimization_id") or args.get("id")

        if not user_id:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"success": False, "error": "user_id is required"}),
                )
            ]
        if not optimization_id:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"success": False, "error": "optimization_id is required"}
                    ),
                )
            ]

        record = get_optimization_from_db(user_id, optimization_id)
        # Fall back to owners who shared this optimization. shared_scopes is
        # backend-injected from grants; only an owner with a matching grant
        # (specific id, or share-all) is searched.
        if not record:
            for s in (args.get("shared_scopes") or []):
                owner = s.get("ownerId")
                gid = s.get("groupId")
                if owner and owner != user_id and (gid is None or gid == optimization_id):
                    record = get_optimization_from_db(owner, optimization_id)
                    if record:
                        break
        if not record:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "success": False,
                            "error": f"Optimization '{optimization_id}' not found",
                        }
                    ),
                )
            ]

        # Strip internal/meta fields the caller doesn't need; everything
        # else flows through (history, test_scores_by_iter, rationales).
        out = {k: v for k, v in record.items() if k not in ("type", "updated_at")}
        out["success"] = True

        return [TextContent(type="text", text=json.dumps(out, indent=2))]

    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"success": False, "error": f"Failed to load optimization: {str(e)}"}
                ),
            )
        ]
