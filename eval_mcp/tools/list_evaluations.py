"""List evaluations from user's .eval log files using Inspect AI's API."""

import json
from pathlib import Path
from typing import Any, Dict, List

from mcp.types import TextContent

from inspect_ai._view.common import list_eval_logs_async
from inspect_ai.log import read_eval_log_async

from eval_mcp.core.user_storage import get_user_log_dir, load_eval_detail


async def handle_list_evaluations(args: Dict[str, Any]) -> List[TextContent]:
    """List evaluations by reading .eval log files from the user's logs directory."""
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

        log_dir = get_user_log_dir(user_id)
        eval_log_infos = await list_eval_logs_async(log_dir)

        if not eval_log_infos:
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

        # Cache pre-computed details per group so multi-model groups only
        # deserialize once (same summary UI/PDF consume).
        detail_cache: Dict[str, Dict[str, Any] | None] = {}

        def _detail_for(run_id: str) -> Dict[str, Any] | None:
            if run_id not in detail_cache:
                try:
                    detail_cache[run_id] = load_eval_detail(user_id, run_id)
                except Exception:
                    detail_cache[run_id] = None
            return detail_cache[run_id]

        evaluations = []
        for info in eval_log_infos[:limit]:
            try:
                log = await read_eval_log_async(info.name, header_only=True)

                run_id = log.eval.run_id
                model_id = log.eval.model
                detail = _detail_for(run_id)
                aggregate = (detail or {}).get("aggregate", {}).get(model_id) or {}

                score_summary: Dict[str, Any] = {"scorer": "jury_scorer", "metrics": {}}
                if "overall" in aggregate:
                    score_summary["metrics"]["overall"] = aggregate["overall"]
                if "byCriterion" in aggregate:
                    score_summary["byCriterion"] = aggregate["byCriterion"]

                # Fall back to raw Inspect metrics only if the aggregate is missing
                # (e.g. detail pre-computation failed).
                if not score_summary["metrics"] and log.results and log.results.scores:
                    for s in log.results.scores:
                        for name, m in s.metrics.items():
                            score_summary["metrics"][name] = m.value

                eval_data = {
                    "id": run_id,
                    "createdAt": str(log.eval.created),
                    "task": log.eval.task,
                    "model": model_id,
                    "totalSamples": log.eval.dataset.samples if log.eval.dataset else 0,
                    "score": score_summary,
                    "status": log.status,
                    "logFile": info.name,
                }

                if log.stats and log.stats.model_usage:
                    total_tokens = {}
                    for model, usage in log.stats.model_usage.items():
                        total_tokens[model] = usage.total_tokens
                    eval_data["totalTokens"] = total_tokens

                evaluations.append(eval_data)
            except Exception:
                continue

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
