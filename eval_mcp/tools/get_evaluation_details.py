"""Get detailed results for a specific evaluation using Inspect AI's API."""

import json
from pathlib import Path
from typing import Any, Dict, List

from mcp.types import TextContent

from inspect_ai._view.common import list_eval_logs_async
from inspect_ai.log import read_eval_log_async

from eval_mcp.core.user_storage import get_user_log_dir


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

        # Search the caller's own dir first, then any owner who shared with
        # them. shared_scopes is injected by the backend from grants (never the
        # model): {ownerId, groupId}, groupId=None = all of that owner's evals.
        # Restricting the foreign search to scopes that match eval_id (or are
        # share-all) is what enforces per-group grants here.
        shared_scopes = args.get("shared_scopes") or []
        search_owners = [user_id]
        for s in shared_scopes:
            owner = s.get("ownerId")
            gid = s.get("groupId")
            if owner and owner != user_id and (gid is None or gid == eval_id):
                if owner not in search_owners:
                    search_owners.append(owner)

        # Find the matching log file by filename or run_id, across owners.
        target_log = None
        for owner in search_owners:
            try:
                owner_infos = await list_eval_logs_async(get_user_log_dir(owner))
            except Exception:
                continue
            for info in owner_infos:
                if eval_id in info.name:
                    target_log = info.name
                    break
                try:
                    log = await read_eval_log_async(info.name, header_only=True)
                    if log.eval.run_id == eval_id:
                        target_log = info.name
                        break
                except Exception:
                    continue
            if target_log:
                break

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

        log = await read_eval_log_async(target_log)

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

        # Sample results (first 10). We surface Score.metadata so callers
        # see the per-criterion vote breakdown (votes_for/total/score) and
        # the improvement_notes that the jury attached when a criterion
        # scored 0. The prompt optimizer reads this metadata when picking
        # failures to feed back to the proposer; the UI uses it to show
        # why a sample failed without re-opening the .eval file.
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
                    score_rows: Dict[str, Any] = {}
                    for k, v in sample.scores.items():
                        row: Dict[str, Any] = {"value": v.value}
                        meta = getattr(v, "metadata", None) or {}
                        if meta.get("criteria_results"):
                            row["criteria_results"] = meta["criteria_results"]
                        score_rows[k] = row
                    s["scores"] = score_rows
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
