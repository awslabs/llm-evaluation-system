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
        from eval_mcp.server import main as mcp_main
        mcp_main()


@main.command()
@click.argument("bucket")
def init(bucket):
    """Set up S3 sharing with one command.

    \b
    Example:
        eval-mcp init my-team-evals
    """
    from eval_mcp.config import set_config_value, get_user
    set_config_value("bucket", bucket)
    user = get_user()
    click.echo(f"Configured S3 sharing:")
    click.echo(f"  bucket: {bucket}")
    click.echo(f"  user:   {user} (auto-detected from AWS identity)")
    click.echo(f"\nLogs will sync to s3://{bucket}/users/{user}/")
    click.echo(f"Shared projects auto-discovered from s3://{bucket}/projects/*/")


@main.command()
@click.option("--port", default=4001, help="Port for the viewer")
def view(port):
    """Open the evaluation results viewer in your browser."""
    from eval_mcp.config import get_bucket
    if get_bucket():
        from eval_mcp.s3_sync import sync_logs_down
        result = sync_logs_down()
        if not result.get("skipped"):
            click.echo(f"Synced {result['synced']} logs from S3")
    from eval_mcp.viewer import start_viewer
    start_viewer(port=port)


@main.command()
@click.option("--port", default=8002, help="Port for HTTP MCP server")
@click.option("--host", default="127.0.0.1", help="Host to bind to")
def serve(host, port):
    """Start as HTTP MCP server with results viewer."""
    import os
    os.environ["EVAL_MCP_TRANSPORT"] = "http"
    os.environ["HOST"] = host
    os.environ["EVAL_MCP_PORT"] = str(port)
    from eval_mcp.server import main as mcp_main
    mcp_main()


@main.command()
def sync():
    """Sync eval logs with S3 (upload personal, download personal + projects)."""
    from eval_mcp.config import get_bucket, get_user
    from eval_mcp.s3_sync import sync_logs_up, sync_logs_down
    if not get_bucket():
        click.echo("No bucket configured. Run: eval-mcp init <bucket-name>")
        return
    up = sync_logs_up()
    down = sync_logs_down()
    click.echo(f"Uploaded {up['synced']} logs to users/{get_user()}/")
    projects = down.get("projects", [])
    if projects:
        click.echo(f"Downloaded {down['synced']} logs from: {', '.join(projects)}")
    else:
        click.echo(f"Downloaded {down['synced']} logs")


@main.command()
@click.argument("project")
def share(project):
    """Share eval logs to a project folder (e.g., eval-mcp share my-project)."""
    from eval_mcp.config import get_bucket
    from eval_mcp.s3_sync import sync_logs_to_project
    if not get_bucket():
        click.echo("No bucket configured. Run: eval-mcp init <bucket-name>")
        return
    result = sync_logs_to_project(project)
    click.echo(f"Shared {result['synced']} logs to projects/{project}/")


@main.group()
def config():
    """Manage eval-mcp configuration."""
    pass


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key, value):
    """Set a config value (e.g., eval-mcp config set region us-west-2)."""
    from eval_mcp.config import set_config_value
    set_config_value(key, value)
    click.echo(f"{key} = {value}")


@config.command("get")
@click.argument("key", required=False)
def config_get(key):
    """Get config value(s)."""
    from eval_mcp.config import get_config
    cfg = get_config()
    if key:
        click.echo(cfg.get(key, "(not set)"))
    else:
        if not cfg:
            click.echo("(no config set)")
        for k, v in cfg.items():
            click.echo(f"{k} = {v}")
