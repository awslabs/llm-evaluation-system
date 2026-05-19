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
@click.option(
    "--ide",
    default=None,
    help="Comma-separated IDE names to install into (claude-code, kiro, vscode, cursor, codex). "
         "Default: auto-detect all installed IDEs.",
)
@click.option("--yes", "-y", is_flag=True, help="Non-interactive: install into all selected IDEs without prompting.")
@click.option("--force", is_flag=True, help="Overwrite an existing 'eval' registration instead of skipping.")
@click.option("--no-warm-cache", is_flag=True, help="Skip the uvx cache warm-up step.")
@click.option(
    "--print-only",
    is_flag=True,
    help="Just print the bundled INSTALL.md guide and exit. For coding agents that want to do the install themselves.",
)
def install(ide, yes, force, no_warm_cache, print_only):
    """Install eval-mcp into the IDEs on this machine.

    \b
    Detects Claude Code, Kiro, VS Code, Cursor, and Codex. Asks which to
    configure (or honors --ide / --yes), registers the MCP server in each,
    and warms the uvx cache so first launch isn't 60s of "disconnected".

    \b
    Examples:
      eval-mcp install                              # detect + ask
      eval-mcp install --yes                        # detect + install all
      eval-mcp install --ide claude-code,kiro --yes # explicit list
      eval-mcp install --print-only                 # print guide, do nothing
    """
    from eval_mcp.installers import REGISTRY

    if print_only:
        _print_install_guide()
        return

    requested = _parse_ide_flag(ide) if ide else None
    targets = _select_targets(REGISTRY, requested=requested, assume_yes=yes)
    if not targets:
        return

    results = [(inst, inst.install(force=force)) for inst in targets]
    _print_summary(results)

    any_installed = any(r.status in ("installed", "replaced") for _, r in results)
    if any_installed and not no_warm_cache:
        _warm_uvx_cache()
    if any_installed:
        click.echo("\nNext steps:")
        for inst, r in results:
            if r.status in ("installed", "replaced"):
                click.echo(f"  • {inst.display}: {inst.restart_hint()}")


def _parse_ide_flag(value: str) -> list[str]:
    """Split `--ide a,b,c` into `["a","b","c"]` and validate each name."""
    from eval_mcp.installers import REGISTRY
    names = [n.strip() for n in value.split(",") if n.strip()]
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        valid = ", ".join(REGISTRY.keys())
        raise click.BadParameter(
            f"unknown IDE(s): {', '.join(unknown)}. Valid: {valid}"
        )
    return names


def _select_targets(registry, *, requested, assume_yes):
    """Decide which installers to run. Returns a list of Installer instances.

    Rules:
      • If --ide given: use that list verbatim (even if not detected — user knows best).
      • Else: detect all, then ask interactively unless --yes.
      • Empty result → print a friendly message and return [].
    """
    if requested:
        return [registry[name] for name in requested]

    detected = [inst for inst in registry.values() if inst.detect()]
    if not detected:
        click.echo("No supported IDEs detected on this machine.")
        click.echo("Supported: " + ", ".join(registry.keys()))
        return []

    click.echo("Detected IDEs:")
    for inst in registry.values():
        marker = "x" if inst in detected else " "
        suffix = "" if inst in detected else "    (not detected)"
        click.echo(f"  [{marker}] {inst.display}{suffix}")

    if assume_yes or len(detected) == 1:
        return detected

    # Interactive: ask which subset
    names = ",".join(i.name for i in detected)
    choice = click.prompt(
        f"\nInstall into [a]ll detected / comma-separated names ({names}) / [q]uit",
        default="a",
    ).strip().lower()
    if choice in ("q", "quit"):
        return []
    if choice in ("a", "all", ""):
        return detected
    picked = _parse_ide_flag(choice)
    return [registry[name] for name in picked]


def _print_summary(results):
    click.echo("\nInstall summary:")
    glyphs = {
        "installed": "✓",
        "replaced": "✓",
        "skipped": "-",
        "failed": "✗",
        "not-detected": "-",
    }
    for inst, r in results:
        g = glyphs.get(r.status, "?")
        line = f"  {g} {inst.display}: {r.status}"
        if r.message:
            line += f" — {r.message}"
        click.echo(line)


def _warm_uvx_cache():
    """Pre-fetch the uvx-cached package so the IDE's first MCP launch is fast."""
    import subprocess

    click.echo("\nWarming uvx cache (first run only, may take ~60s)...", err=True)
    try:
        subprocess.run(
            ["uvx", "--from", "llm-evaluation-system", "eval-mcp", "--help"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=True, timeout=180,
        )
        click.echo("uvx cache warmed.", err=True)
    except FileNotFoundError:
        click.echo(
            "uvx not found on PATH. Install uv first: "
            "curl -LsSf https://astral.sh/uv/install.sh | sh",
            err=True,
        )
    except subprocess.TimeoutExpired:
        click.echo(
            "uvx warm-up timed out after 3 min — the IDE may need a longer "
            "timeout on first launch.",
            err=True,
        )
    except subprocess.CalledProcessError as e:
        click.echo(
            f"uvx warm-up failed with exit {e.returncode} — the IDE may "
            "slow-start on first launch.",
            err=True,
        )


def _print_install_guide():
    """Back-compat: ``--print-only`` still emits the bundled INSTALL.md
    so coding agents that prefer to drive the install themselves can."""
    from pathlib import Path

    guide = Path(__file__).parent / "INSTALL.md"
    if not guide.exists():
        click.echo(
            "INSTALL.md not bundled with this install — reinstall the package.",
            err=True,
        )
        sys.exit(1)
    click.echo(guide.read_text())


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
