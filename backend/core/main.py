"""Main entry point for the Promptfoo MCP CLI."""

import asyncio
import sys

import click
from rich.console import Console

from .mcp_client import PromptfooMCPClient
from .chat import ChatInterface

console = Console()


@click.command()
@click.option("--debug", is_flag=True, help="Enable debug logging")
@click.option("--region", default="us-west-2", help="AWS region for Bedrock (default: us-west-2)")
@click.version_option(version="0.1.0")
def main(debug: bool, region: str) -> None:
    """Promptfoo MCP CLI - Interactive tool for LLM evaluation."""
    console.print("[bold blue]Promptfoo MCP CLI[/bold blue]")
    console.print("Starting interactive chat mode...\n")

    try:
        asyncio.run(run_chat(debug, region))
    except KeyboardInterrupt:
        console.print("\n[yellow]Goodbye![/yellow]")
        sys.exit(0)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        if debug:
            raise
        sys.exit(1)


async def run_chat(debug: bool, region: str) -> None:
    """Run the interactive chat loop."""
    client = PromptfooMCPClient(region=region)

    try:
        # Connect to MCP servers
        console.print("[dim]Connecting to MCP servers...[/dim]")
        await client.connect()

        # Start interactive chat
        chat = ChatInterface(client, debug=debug, region=region)
        await chat.start()

    except ConnectionError as e:
        console.print(f"[red]Connection failed:[/red] {e}")
        console.print("\n[yellow]Make sure Node.js is installed and promptfoo is available.[/yellow]")
        console.print("Test with: [cyan]npx promptfoo@0.119.0 --version[/cyan]")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        if debug:
            raise
        sys.exit(1)
    finally:
        # Clean up connection
        if client.sessions:
            console.print("\n[dim]Disconnecting...[/dim]")
            await client.disconnect()


if __name__ == "__main__":
    main()
