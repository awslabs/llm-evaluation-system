"""Get detailed results for a specific evaluation using Inspect AI's API."""

import json
from pathlib import Path
from typing import Any, Dict, List

from mcp.types import TextContent

from inspect_ai.log import read_eval_log

from backend.core.user_storage import get_user_dir


async def handle_get_evaluation_details(args: Dict[str, Any]) -> List[TextContent]:
    """Get detailed results for a specific evaluation from .eval log files."""
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

        user_dir = get_user_dir(user_id)
        log_dir = user_dir / "logs"

        if not log_dir.exists():
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"success": False, "error": "No evaluations found."}),
                )
            ]

        # Find the matching log file by filename stem or run_id
        target_log = None
        for log_file in log_dir.glob("*.eval"):
            if log_file.stem == eval_id or eval_id in log_file.stem:
                target_log = log_file
                break
            try:
                log = read_eval_log(str(log_file), header_only=True)
                if log.eval.run_id == eval_id:
                    target_log = log_file
                    break
            except Exception:
                continue

        if not target_log:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "error": f"Evaluation '{eval_id}' not found. Use list_evaluations to see available IDs.",
                    }),
                )
            ]

        log = read_eval_log(str(target_log))

        scores_summary = []
        if log.results and log.results.scores:
            for s in log.results.scores:
                metrics = {}
                for name, m in s.metrics.items():
                    metrics[name] = m.value
                scores_summary.append({"scorer": s.name, "metrics": metrics})

        token_usage = {}
        if log.stats and log.stats.model_usage:
            for model, usage in log.stats.model_usage.items():
                token_usage[model] = {
                    "input": usage.input_tokens,
                    "output": usage.output_tokens,
                    "total": usage.total_tokens,
                }

        eval_data = {
            "id": log.eval.run_id,
            "createdAt": str(log.eval.created),
            "task": log.eval.task,
            "model": log.eval.model,
            "status": log.status,
            "summary": {
                "totalSamples": len(log.samples) if log.samples else 0,
                "scores": scores_summary,
                "modelUsage": token_usage,
            },
        }

        # Sample results (first 10)
        sample_results = []
        if log.samples:
            for sample in log.samples[:10]:
                s: Dict[str, Any] = {
                    "id": sample.id,
                    "input": str(sample.input)[:200],
                    "target": str(sample.target)[:200],
                }
                if sample.output and sample.output.completion:
                    s["output"] = sample.output.completion[:200]
                if sample.scores:
                    s["scores"] = {
                        k: v.value for k, v in sample.scores.items()
                    }
                sample_results.append(s)

        eval_data["sampleResults"] = sample_results

        return [TextContent(type="text", text=json.dumps({
            "success": True,
            **eval_data,
        }, indent=2))]

    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"success": False, "error": f"Failed to get evaluation details: {str(e)}"}),
            )
        ]
