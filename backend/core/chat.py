"""Interactive chat/REPL interface."""


from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from rich.console import Console
from rich.markdown import Markdown

from .mcp_client import MultiMCPClient
from .bedrock_client import BedrockClient
from .agent import Agent

console = Console()


class ChatInterface:
    """Interactive chat interface for eval MCP."""

    def __init__(self, mcp_client: MultiMCPClient, debug: bool = False, region: str = "us-west-2") -> None:
        """Initialize chat interface."""
        self.mcp_client = mcp_client
        self.debug = debug
        self.history = InMemoryHistory()
        self.session = PromptSession(history=self.history)

        # Initialize Bedrock and Agent
        self.bedrock = BedrockClient(region=region)
        self.agent = Agent(self.bedrock, mcp_client, debug=debug)

    async def start(self) -> None:
        """Start the interactive chat loop."""
        num_servers = len(self.mcp_client.sessions)
        server_names = ", ".join(self.mcp_client.sessions.keys())
        console.print(f"[green]Connected to {num_servers} MCP server(s): {server_names}[/green]")
        console.print("[green]Using Claude Sonnet 4.0 via AWS Bedrock[/green]")
        console.print("Chat naturally - Claude has access to all MCP tools!")
        console.print("Type 'exit' to quit, 'clear' to clear conversation history\n")

        while True:
            try:
                # Get user input
                user_input = await self.session.prompt_async("you> ")

                if not user_input.strip():
                    continue

                # Handle special commands
                if user_input.lower() in ["exit", "quit", "q"]:
                    break
                elif user_input.lower() == "clear":
                    self.agent.clear_history()
                    console.print("[dim]Conversation history cleared.[/dim]\n")
                    continue

                # Send to agent and get response
                await self._process_query(user_input)

            except KeyboardInterrupt:
                break
            except EOFError:
                break
            except Exception as e:
                console.print(f"[red]Error:[/red] {e}")
                if self.debug:
                    raise

    async def _process_query(self, query: str) -> None:
        """Process user query through the agent."""
        try:
            # Show thinking indicator
            with console.status("[dim]Claude is thinking...[/dim]", spinner="dots"):
                response = await self.agent.run_conversation_turn(query)

            # Display Claude's response
            console.print("\n[bold cyan]Claude:[/bold cyan]")
            console.print(Markdown(response))
            console.print()

        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            if self.debug:
                raise
