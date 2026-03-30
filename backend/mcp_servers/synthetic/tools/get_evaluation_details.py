"""Get detailed results for a specific evaluation."""

import json
import sqlite3
from typing import Any, Dict, List

from mcp.types import TextContent

from backend.core.user_storage import get_user_promptfoo_dir


async def handle_get_evaluation_details(args: Dict[str, Any]) -> List[TextContent]:
    """Get detailed results for a specific evaluation from eval_results table."""
    try:
        eval_id = args.get("evalId")
        user_id = args.get("user_id")

        if not user_id:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"success": False, "error": "user_id is required"}),
                )
            ]
        if not eval_id:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"success": False, "error": "evalId is required"}),
                )
            ]

        user_dir = get_user_promptfoo_dir(user_id)
        db_path = user_dir / "promptfoo.db"

        if not db_path.exists():
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"success": False, "error": "No evaluations found."}),
                )
            ]

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Get eval metadata
        cursor.execute(
            "SELECT id, created_at, description, config FROM evals WHERE id = ?",
            (eval_id,),
        )
        eval_row = cursor.fetchone()
        if not eval_row:
            conn.close()
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "error": f"Evaluation '{eval_id}' not found. Use list_evaluations to see available IDs.",
                    }),
                )
            ]

        eval_data = {
            "id": eval_row["id"],
            "createdAt": eval_row["created_at"],
            "description": eval_row["description"],
        }

        # Parse config for provider info
        if eval_row["config"]:
            try:
                config = json.loads(eval_row["config"])
                providers = config.get("providers", [])
                eval_data["providers"] = [
                    p.get("id", p) if isinstance(p, dict) else p
                    for p in providers
                ]
            except json.JSONDecodeError:
                pass

        # Get overall stats from eval_results
        cursor.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as pass_count,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as fail_count,
                SUM(cost) as total_cost,
                AVG(latency_ms) as avg_latency_ms
            FROM eval_results
            WHERE eval_id = ?
        """, (eval_id,))
        stats = cursor.fetchone()

        total = stats["total"]
        pass_count = stats["pass_count"] or 0
        eval_data["summary"] = {
            "totalResults": total,
            "passCount": pass_count,
            "failCount": stats["fail_count"] or 0,
            "passRate": f"{pass_count / total * 100:.0f}%" if total > 0 else "N/A",
            "totalCost": round(stats["total_cost"], 4) if stats["total_cost"] else 0,
            "avgLatencyMs": round(stats["avg_latency_ms"]) if stats["avg_latency_ms"] else 0,
        }

        # Get per-provider breakdown
        cursor.execute("""
            SELECT
                json_extract(provider, '$.id') as provider_id,
                COUNT(*) as total,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as pass_count,
                SUM(cost) as cost,
                AVG(latency_ms) as avg_latency_ms,
                AVG(score) as avg_score
            FROM eval_results
            WHERE eval_id = ?
            GROUP BY provider_id
        """, (eval_id,))

        provider_stats = []
        for row in cursor.fetchall():
            p_total = row["total"]
            p_pass = row["pass_count"] or 0
            provider_stats.append({
                "provider": row["provider_id"],
                "total": p_total,
                "passCount": p_pass,
                "passRate": f"{p_pass / p_total * 100:.0f}%" if p_total > 0 else "N/A",
                "avgScore": round(row["avg_score"], 3) if row["avg_score"] else 0,
                "cost": round(row["cost"], 4) if row["cost"] else 0,
                "avgLatencyMs": round(row["avg_latency_ms"]) if row["avg_latency_ms"] else 0,
            })
        eval_data["providerBreakdown"] = provider_stats

        # Get sample results (first 10)
        cursor.execute("""
            SELECT
                test_idx, provider, success, score, cost, latency_ms,
                named_scores, grading_result,
                json_extract(test_case, '$.vars.question') as question,
                json_extract(response, '$.output') as output
            FROM eval_results
            WHERE eval_id = ?
            ORDER BY test_idx, prompt_idx
            LIMIT 10
        """, (eval_id,))

        sample_results = []
        for row in cursor.fetchall():
            sample = {
                "testIdx": row["test_idx"],
                "provider": json.loads(row["provider"]).get("id") if row["provider"] else None,
                "success": bool(row["success"]),
                "score": row["score"],
                "cost": round(row["cost"], 6) if row["cost"] else 0,
                "latencyMs": row["latency_ms"],
            }
            if row["question"]:
                sample["question"] = row["question"][:150]
            if row["output"]:
                sample["outputPreview"] = row["output"][:200]
            if row["named_scores"]:
                try:
                    sample["namedScores"] = json.loads(row["named_scores"])
                except json.JSONDecodeError:
                    pass
            sample_results.append(sample)

        eval_data["sampleResults"] = sample_results

        conn.close()

        return [TextContent(type="text", text=json.dumps({
            "success": True,
            "evaluation": eval_data,
        }, indent=2))]

    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"success": False, "error": f"Failed to get evaluation details: {str(e)}"}),
            )
        ]
