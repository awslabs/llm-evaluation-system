"""Generate a PDF report for an evaluation.

Saves to ~/.eval-mcp/users/{user}/reports/{group_id}.pdf so the viewer's
Download button can fetch it.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.types import TextContent

from eval_mcp.core.bedrock_client import BedrockClient
from eval_mcp.core.eval_results import precompute_eval_results
from eval_mcp.core.pdf_report import generate_pdf_report
from eval_mcp.core.user_storage import get_user_dir, load_eval_detail

logger = logging.getLogger(__name__)


def _reports_dir(user_id: str) -> Path:
    d = get_user_dir(user_id) / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_id(group_id: str) -> str:
    return group_id.replace("/", "_").replace("\\", "_")


async def handle_generate_report(args: Dict[str, Any]) -> List[TextContent]:
    """Generate and save a PDF report for an evaluation group."""
    user_id = args.get("user_id") or "local"
    group_id = args.get("group_id")
    context = args.get("context")
    monthly_volume = int(args.get("monthly_volume", 10000))

    if not group_id:
        return [TextContent(type="text", text=json.dumps({
            "success": False,
            "error": "group_id is required",
        }))]

    try:
        detail = load_eval_detail(user_id, group_id)
        if not detail:
            await precompute_eval_results(user_id)
            detail = load_eval_detail(user_id, group_id)
        # Not the caller's own eval — try owners who shared this group. The
        # report PDF is still written to the CALLER's own dir below (their
        # artifact); we only read the shared owner's detail data.
        if not detail:
            shared_scopes = args.get("shared_scopes") or []
            for s in shared_scopes:
                owner = s.get("ownerId")
                gid = s.get("groupId")
                if owner and owner != user_id and (gid is None or gid == group_id):
                    detail = load_eval_detail(owner, group_id)
                    if detail:
                        break
        if not detail:
            return [TextContent(type="text", text=json.dumps({
                "success": False,
                "error": f"Evaluation group not found: {group_id}",
            }))]

        transcript = None
        if context:
            transcript = [{"role": "user", "content": context}]

        bedrock = BedrockClient()
        pdf_bytes = await generate_pdf_report(
            detail=detail,
            bedrock=bedrock,
            transcript=transcript,
            monthly_volume=monthly_volume,
        )

        reports_dir = _reports_dir(user_id)
        filename = f"report_{_safe_id(group_id)}.pdf"
        pdf_path = reports_dir / filename
        pdf_path.write_bytes(pdf_bytes)

        # On EKS the download endpoint reads from S3 (DATA_BUCKET env), but
        # the s3_sync.replicate_async path uses a separate ~/.eval-mcp config
        # bucket — different mechanism, not set in deployed pods. Without an
        # explicit upload here, the file lives on the generating pod's local
        # disk only, and any other replica returns 404. Mirror what the
        # backend's own /report/pdf does (compare.py:212-219).
        try:
            from eval_mcp.core.user_storage import (
                _s3_enabled, _get_s3_client, DATA_BUCKET,
            )
            if _s3_enabled():
                key = f"users/{user_id}/reports/{filename}"
                _get_s3_client().put_object(
                    Bucket=DATA_BUCKET,
                    Key=key,
                    Body=pdf_bytes,
                    ContentType="application/pdf",
                )
        except Exception as e:
            logger.warning(f"Failed to upload report to S3: {e}")

        # Also try the s3_sync replicate path for users who set up team
        # sharing via `eval-mcp init` (separate bucket from DATA_BUCKET).
        try:
            from eval_mcp.s3_sync import replicate_async
            replicate_async(pdf_path, user_id=user_id)
        except Exception:
            pass

        return [TextContent(type="text", text=json.dumps({
            "success": True,
            "groupId": group_id,
            "path": str(pdf_path),
            "sizeBytes": len(pdf_bytes),
            "downloadUrl": f"/api/compare/report/{group_id}",
            "message": f"Report saved. Download via /api/compare/report/{group_id}",
        }, indent=2))]

    except Exception as e:
        logger.exception("report generation failed")
        return [TextContent(type="text", text=json.dumps({
            "success": False,
            "error": f"Failed to generate report: {str(e)}",
        }))]
