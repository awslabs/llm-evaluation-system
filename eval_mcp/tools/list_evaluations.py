"""List evaluations from user's .eval log files using Inspect AI's API."""

import json
from pathlib import Path
from typing import Any, Dict, List

from mcp.types import TextContent

from inspect_ai._view.common import list_eval_logs_async
from inspect_ai.log import read_eval_log_async

from eval_mcp.core.user_storage import get_user_log_dir, load_eval_detail


async def handle_list_evaluations(args: Dict[str, Any]) -> List[TextContent]:
    """List evaluations with pagination and optional markdown format.

    Args (from `args` dict):
        user_id: required
        limit: page size, default 20
        offset: page start, default 0
        response_format: "json" (default — eval payloads are heavy) or "markdown"
    """
    try:
        user_id = args.get("user_id")
        limit = max(1, int(args.get("limit", 20) or 20))
        offset = max(0, int(args.get("offset", 0) or 0))
        response_format = (args.get("response_format") or "json").lower()

        if not user_id:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"success": False, "error": "user_id is required"}),
                )
            ]

        log_dir = get_user_log_dir(user_id)
        eval_log_infos = await list_eval_logs_async(log_dir)

        # Append evals shared with this caller. shared_scopes is injected by the
        # backend from grants (never the model): a list of {ownerId, groupId},
        # groupId=None meaning all of that owner's evals. We tag each shared
        # info with the owner so detail/report lookups can scope correctly.
        shared_scopes = args.get("shared_scopes") or []
        owner_of_info: Dict[str, str] = {}
        if shared_scopes:
            by_owner: Dict[str, set] = {}
            for s in shared_scopes:
                by_owner.setdefault(s.get("ownerId"), set()).add(s.get("groupId"))
            for owner, gids in by_owner.items():
                if not owner or owner == user_id:
                    continue
                try:
                    owner_infos = await list_eval_logs_async(get_user_log_dir(owner))
                except Exception:
                    continue
                allow_all = None in gids
                for oi in owner_infos:
                    try:
                        if not allow_all:
                            olog = await read_eval_log_async(oi.name, header_only=True)
                            if olog.eval.run_id not in gids:
                                continue
                        eval_log_infos.append(oi)
                        owner_of_info[oi.name] = owner
                    except Exception:
                        continue

        if not eval_log_infos:
            empty_text = (
                json.dumps({
                    "success": True,
                    "total": 0,
                    "count": 0,
                    "offset": offset,
                    "has_more": False,
                    "next_offset": None,
                    "evaluations": [],
                    "message": "No evaluations found. Run an evaluation first.",
                })
                if response_format == "json"
                else "No evaluations found. Run an evaluation first."
            )
            return [TextContent(type="text", text=empty_text)]

        total = len(eval_log_infos)
        page_infos = eval_log_infos[offset : offset + limit]
        has_more = offset + len(page_infos) < total
        next_offset = offset + len(page_infos) if has_more else None

        # Cache pre-computed details per group so multi-model groups only
        # deserialize once (same summary UI/PDF consume).
        detail_cache: Dict[str, Dict[str, Any] | None] = {}

        def _detail_for(run_id: str, owner: str) -> Dict[str, Any] | None:
            cache_key = f"{owner}:{run_id}"
            if cache_key not in detail_cache:
                try:
                    detail_cache[cache_key] = load_eval_detail(owner, run_id)
                except Exception:
                    detail_cache[cache_key] = None
            return detail_cache[cache_key]

        evaluations = []
        for info in page_infos:
            try:
                log = await read_eval_log_async(info.name, header_only=True)

                run_id = log.eval.run_id
                model_id = log.eval.model
                owner = owner_of_info.get(info.name, user_id)
                detail = _detail_for(run_id, owner)
                # Score-only runs log model="none/none"; the detail builder
                # relabels to "pre-generated" — surface the same label here
                # so the list view doesn't say one thing and the detail view
                # another.
                if detail and detail.get("scoreOnly") and model_id == "none/none":
                    display_model = (detail.get("models") or [model_id])[0]
                else:
                    display_model = model_id
                aggregate = (detail or {}).get("aggregate", {}).get(display_model) or {}

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
                    "model": display_model,
                    "totalSamples": log.eval.dataset.samples if log.eval.dataset else 0,
                    "score": score_summary,
                    "status": log.status,
                    "logFile": info.name,
                }
                # Mark shared (non-own) evals so the chat agent can attribute them.
                if owner != user_id:
                    eval_data["owner"] = owner
                    eval_data["shared"] = True

                if log.stats and log.stats.model_usage:
                    total_tokens = {}
                    for model, usage in log.stats.model_usage.items():
                        total_tokens[model] = usage.total_tokens
                    eval_data["totalTokens"] = total_tokens

                evaluations.append(eval_data)
            except Exception:
                continue

        if response_format == "markdown":
            output = f"Found {total} evaluation(s) — showing {offset + 1}-{offset + len(evaluations)}:\n\n"
            for e in evaluations:
                overall = e["score"]["metrics"].get("overall")
                overall_str = f"{overall:.2f}" if isinstance(overall, (int, float)) else "—"
                output += f"🧪 **{e['task']}** ({e['model']})\n"
                output += f"   ID: {e['id']}\n"
                output += f"   Status: {e['status']} · Samples: {e['totalSamples']} · Overall: {overall_str}\n"
                output += f"   Created: {e['createdAt']}\n\n"
            if has_more:
                output += f"More available — pass offset={next_offset} to see the next page.\n"
            return [TextContent(type="text", text=output)]

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": True,
                        "total": total,
                        "count": len(evaluations),
                        "offset": offset,
                        "has_more": has_more,
                        "next_offset": next_offset,
                        "evaluations": evaluations,
                    },
                    indent=2,
                ),
            )
        ]

    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"success": False, "error": f"Failed to list evaluations: {str(e)}"}),
            )
        ]
