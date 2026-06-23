"""List optimization runs from the user's persisted optimizations store.

Mirrors ``list_evaluations`` shape: pagination + optional markdown
format, but reads from a per-user JSON store rather than Inspect
``.eval`` logs. Each iteration inside an optimization IS a real Inspect
eval (and therefore also shows up in ``list_evaluations``), but the
optimization-level record — winner, history, rationales, train/test
split — is its own artifact and lives here.
"""

import json
from typing import Any, Dict, List

from mcp.types import TextContent

from eval_mcp.core.user_storage import list_optimizations_from_db, merge_shared_rows


async def handle_list_optimizations(args: Dict[str, Any]) -> List[TextContent]:
    """Args (from ``args`` dict):
        user_id: required
        limit: page size, default 20
        offset: page start, default 0
        search: optional substring filter (dataset / initial / winner prompt)
        response_format: "json" (default) or "markdown"
    """
    try:
        user_id = args.get("user_id")
        limit = max(1, int(args.get("limit", 20) or 20))
        offset = max(0, int(args.get("offset", 0) or 0))
        search = (args.get("search") or "").strip()
        response_format = (args.get("response_format") or "json").lower()

        if not user_id:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"success": False, "error": "user_id is required"}),
                )
            ]

        rows = list_optimizations_from_db(user_id, search_term=search)
        rows.extend(merge_shared_rows(
            user_id, args.get("shared_scopes"),
            lambda owner: list_optimizations_from_db(owner, search_term=search),
        ))
        total = len(rows)
        page = rows[offset : offset + limit]
        has_more = offset + len(page) < total
        next_offset = offset + len(page) if has_more else None

        if response_format == "markdown":
            if not page:
                return [TextContent(type="text", text="No optimization runs yet. Use optimize_prompt to start one.")]
            lines = [f"Found {total} optimization run(s) — showing {offset + 1}-{offset + len(page)}:\n"]
            for r in page:
                test_score = r.get("winner_test_score")
                score_str = f"{test_score:.2f}" if isinstance(test_score, (int, float)) else "—"
                lines.append(f"⚙️  **{r['dataset']}** ({r['judge']})")
                lines.append(f"   ID: {r['id']}")
                lines.append(
                    f"   Status: {r['status']} · Iter winner: {r.get('winner_iter')} · Test score: {score_str}"
                )
                lines.append("")
            if has_more:
                lines.append(f"More available — pass offset={next_offset} for the next page.")
            return [TextContent(type="text", text="\n".join(lines))]

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
                        "optimizations": page,
                    },
                    indent=2,
                ),
            )
        ]

    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"success": False, "error": f"Failed to list optimizations: {str(e)}"}),
            )
        ]
