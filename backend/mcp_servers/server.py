#!/usr/bin/env python3
"""Thin compatibility wrapper.

The canonical MCP server lives in `eval_mcp.server`. This module exists so
existing deployments that invoke `python -m backend.mcp_servers.server`
(see `local/compose.yaml`, `helm/eval/templates/deployment.yaml`) keep
working without redeploying.

New callers should import from `eval_mcp.server` directly.
"""
from eval_mcp.server import main, mcp  # noqa: F401

if __name__ == "__main__":
    main()
