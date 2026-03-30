"""List evaluations from user's promptfoo database."""

import json
import sqlite3
from typing import Any, Dict, List

from mcp.types import TextContent

from backend.core.user_storage import get_user_promptfoo_dir


async def handle_list_evaluations(args: Dict[str, Any]) -> List[TextContent]:
    """List evaluations with summary stats from eval_results table."""
    try:
        user_id = args.get("user_id")
        limit = args.get("limit", 20)

        if not user_id:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"success": False, "error": "user_id is required"}),
                )
            ]

        user_dir = get_user_promptfoo_dir(user_id)
        db_path = user_dir / "promptfoo.db"

        if not db_path.exists():
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "success": True,
                        "evaluations": [],
                        "message": "No evaluations found. Run an evaluation first.",
                    }),
                )
            ]

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        try:
            cursor.execute("""
                SELECT
                    e.id,
                    e.created_at,
                    e.description,
                    e.config,
                    COUNT(er.id) as total_results,
                    SUM(CASE WHEN er.success = 1 THEN 1 ELSE 0 END) as pass_count,
                    SUM(CASE WHEN er.success = 0 THEN 1 ELSE 0 END) as fail_count,
                    SUM(er.cost) as total_cost
                FROM evals e
                LEFT JOIN eval_results er ON e.id = er.eval_id
                GROUP BY e.id
                ORDER BY e.created_at DESC
                LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            conn.close()
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "success": True,
                        "evaluations": [],
                        "message": "No evaluations found or database schema not recognized.",
                    }),
                )
            ]

        evaluations = []
        for row in rows:
            total = row["total_results"]
            pass_count = row["pass_count"] or 0
            eval_data = {
                "id": row["id"],
                "createdAt": row["created_at"],
                "description": row["description"],
                "totalResults": total,
                "passCount": pass_count,
                "failCount": (row["fail_count"] or 0),
                "passRate": f"{pass_count / total * 100:.0f}%" if total > 0 else "N/A",
                "totalCost": round(row["total_cost"], 4) if row["total_cost"] else 0,
            }

            if row["config"]:
                try:
                    config = json.loads(row["config"])
                    providers = config.get("providers", [])
                    eval_data["providers"] = [
                        p.get("id", p) if isinstance(p, dict) else p
                        for p in providers
                    ]
                except json.JSONDecodeError:
                    pass

            evaluations.append(eval_data)

        conn.close()

        return [TextContent(type="text", text=json.dumps({
            "success": True,
            "evaluations": evaluations,
            "total": len(evaluations),
        }, indent=2))]

    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"success": False, "error": f"Failed to list evaluations: {str(e)}"}),
            )
        ]
