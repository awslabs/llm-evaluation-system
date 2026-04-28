"""List evaluations from user's .eval log files using Inspect AI's API."""

import json
from pathlib import Path
from typing import Any, Dict, List

from mcp.types import TextContent

from inspect_ai._view.common import list_eval_logs_async
from inspect_ai.log import read_eval_log_async

from backend.core.user_storage import get_user_log_dir


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

        evaluations = []
        for info in eval_log_infos[:limit]:
            try:
                log = await read_eval_log_async(info.name, header_only=True)

                scores_summary = []
                if log.results and log.results.scores:
                    for s in log.results.scores:
                        metrics = {}
                        for name, m in s.metrics.items():
                            metrics[name] = m.value
                        scores_summary.append({"scorer": s.name, "metrics": metrics})

                eval_data = {
                    "id": log.eval.run_id,
                    "createdAt": str(log.eval.created),
                    "task": log.eval.task,
                    "model": log.eval.model,
                    "totalSamples": log.eval.dataset.samples if log.eval.dataset else 0,
                    "scores": scores_summary,
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
