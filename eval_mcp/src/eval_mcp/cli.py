"""CLI entry point for eval-mcp."""

import sys

import click


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx):
    """MCP server for LLM evaluation using Inspect AI.

    Run without subcommand to start as stdio MCP server (for Claude Code).
    """
    if ctx.invoked_subcommand is None:
        from eval_mcp.server import mcp
        mcp.run(transport="stdio")


@main.command()
@click.option("--port", default=4001, help="Port for the viewer")
def view(port):
    """Open the evaluation results viewer in your browser."""
    from eval_mcp.viewer import start_viewer
    start_viewer(port=port)


@main.command()
@click.option("--port", default=8002, help="Port for HTTP MCP server")
@click.option("--host", default="127.0.0.1", help="Host to bind to")
def serve(host, port):
    """Start as HTTP MCP server with results viewer."""
    import uvicorn
    from eval_mcp.server import mcp

    app = mcp.streamable_http_app()
    print(f"Starting Eval MCP Server on http://{host}:{port}/mcp")
    uvicorn.run(app, host=host, port=port, log_level="info")
