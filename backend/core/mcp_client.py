"""MCP client for connecting to multiple MCP servers."""

import asyncio
import json
import logging
import os
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def setup_mcp_logging(log_dir: str = "backend/logs") -> logging.Logger:
    """Set up structured logging for MCP tool calls and notifications.

    Logs to stdout (Kubernetes captures this and sends to CloudWatch).

    Returns:
        Configured logger instance
    """
    # Create logger
    logger = logging.getLogger("mcp_tools")
    logger.setLevel(logging.DEBUG)

    # Avoid duplicate handlers
    if logger.handlers:
        return logger

    # Console handler (stdout) - Kubernetes best practice
    console_handler = logging.StreamHandler()

    # JSON formatter for structured logs
    formatter = logging.Formatter(
        '{"timestamp": "%(asctime)s", "level": "%(levelname)s", "message": %(message)s}',
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    console_handler.setFormatter(formatter)

    logger.addHandler(console_handler)

    return logger


class MultiMCPClient:
    """Client for interacting with multiple MCP servers with auto-reconnection."""

    def __init__(self, working_dir: Optional[str] = None, region: str = "us-west-2") -> None:
        """Initialize the MCP client with multiple servers.

        Args:
            working_dir: Working directory for eval data
            region: AWS region for Bedrock
        """
        self.sessions: Dict[str, ClientSession] = {}
        self._exit_stacks: Dict[str, AsyncExitStack] = {}  # Per-server exit stacks
        self.logger = setup_mcp_logging()
        self.user_id: Optional[str] = None  # Set per-request for user isolation
        self._reconnect_lock = asyncio.Lock()

        # Use current working directory if not specified
        cwd = working_dir or os.getcwd()

        # Get current environment and merge with custom vars
        env = os.environ.copy()
        env["INSPECT_LOG_DIR"] = cwd
        env["AWS_REGION"] = region

        # Single unified MCP server
        self._server_configs = {
            "eval": {
                "type": "http",
                "url": os.environ["EVAL_MCP_URL"],
            },
        }

    async def _connect_server(self, server_name: str, max_retries: int = 10, base_delay: float = 0.5) -> bool:
        """Connect to a single MCP server with exponential backoff.

        Args:
            server_name: Name of the server to connect to
            max_retries: Maximum connection attempts
            base_delay: Initial delay between retries (doubles each attempt)

        Returns:
            True if connection succeeded, False otherwise
        """
        server_config = self._server_configs.get(server_name)
        if not server_config:
            return False

        delay = base_delay
        for attempt in range(max_retries):
            try:
                if attempt == 0:
                    self.logger.info(f"Connecting to {server_name} server...")
                else:
                    self.logger.info(f"Connecting to {server_name} server (attempt {attempt + 1}/{max_retries})...")

                # Create a new exit stack for this server
                exit_stack = AsyncExitStack()

                if server_config["type"] == "http":
                    # Connect via HTTP with extended timeout for long-running evaluations
                    read, write, _ = await exit_stack.enter_async_context(
                        streamablehttp_client(
                            server_config["url"],
                            timeout=3600.0,  # 1 hour for connection/request
                            sse_read_timeout=7200.0  # 2 hours for SSE streaming
                        )
                    )
                else:
                    raise ValueError(f"Unknown transport type: {server_config['type']}")

                # Create and initialize session
                session = await exit_stack.enter_async_context(
                    ClientSession(read, write)
                )

                # Initialize the connection
                await session.initialize()

                # Store session and exit stack
                self.sessions[server_name] = session
                self._exit_stacks[server_name] = exit_stack
                self.logger.info(f"Connected to {server_name}")
                return True

            except Exception as e:
                if attempt < max_retries - 1:
                    self.logger.warning(f"Connection to {server_name} failed ({e}), retrying in {delay:.1f}s...")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 10.0)  # Cap at 10 seconds
                else:
                    self.logger.error(f"Failed to connect to {server_name} server after {max_retries} attempts: {e}")
                    return False

        return False

    def claim_ownership(self) -> None:
        """Mark the current task as the sole owner of this client's
        connection lifecycle. Future calls to `reconnect_server` from
        any other task will refuse to close the anyio scope — closing
        a scope from a foreign task raises CancelledError on the owner
        and (under the previous architecture) brought the whole pod
        down. Other tasks should signal the owner via a queue rather
        than call reconnect_server directly.
        """
        self._owner_task = asyncio.current_task()

    async def reconnect_server(
        self, server_name: str, max_retries: int = 10
    ) -> bool:
        """Reconnect to a specific MCP server.

        Args:
            server_name: Name of the server to reconnect
            max_retries: How many connect attempts before giving up.
                Default 10 covers cold-start with backoff. Callers in
                latency-sensitive paths (e.g. chat cancel cleanup)
                pass a smaller value so a flaky MCP can't pile up
                ~9 minutes of retry behind the reconnect_lock.

        Returns:
            True if reconnection succeeded.

        Must run in the task that originally connected the client.
        See `claim_ownership`. Calling from a foreign task is treated
        as a bug and refused (returns False) — closing the exit stack
        cross-task triggers anyio CancelledError on the owner and
        previously killed the pod on every eval cancel.
        """
        owner = getattr(self, "_owner_task", None)
        if owner is not None:
            current = asyncio.current_task()
            if current is not None and current is not owner:
                self.logger.warning(
                    "reconnect_server(%s) refused: called from task %r, "
                    "owner is %r. Internal auto-reconnect paths (list_tools, "
                    "call_tool) hit this when run from the agent task; the "
                    "tool call error will propagate instead of silently "
                    "trying to recover (which would crash the pod).",
                    server_name,
                    current.get_name(),
                    owner.get_name(),
                )
                return False

        async with self._reconnect_lock:
            # Clean up old connection if exists
            if server_name in self._exit_stacks:
                try:
                    await self._exit_stacks[server_name].aclose()
                except Exception:
                    pass
                del self._exit_stacks[server_name]

            if server_name in self.sessions:
                del self.sessions[server_name]

            # Try to reconnect
            self.logger.info(f"Reconnecting to {server_name}...")
            return await self._connect_server(server_name, max_retries=max_retries)

    async def connect(self) -> None:
        """Connect to all MCP servers."""
        if self.sessions:
            return  # Already connected

        try:
            # Connect to each server sequentially with a small delay
            for server_name in self._server_configs.keys():
                await self._connect_server(server_name)
                await asyncio.sleep(0.5)

            if not self.sessions:
                raise ConnectionError("Failed to connect to any MCP servers")

        except Exception as e:
            # Clean up on error
            await self.disconnect()
            raise ConnectionError(f"Failed to connect to MCP servers: {e}")

    async def disconnect(self) -> None:
        """Disconnect from all MCP servers."""
        # Close all exit stacks (which closes sessions)
        for server_name, exit_stack in list(self._exit_stacks.items()):
            try:
                await exit_stack.aclose()
            except Exception as e:
                self.logger.warning(f"Error closing {server_name}: {e}")

        self.sessions = {}
        self._exit_stacks = {}

    def set_user_id(self, user_id: str) -> None:
        """Set the user ID for auto-injection into tool calls.

        Args:
            user_id: User ID for storage isolation
        """
        self.user_id = user_id

    async def _shared_scopes(self, resource_type: str = "eval") -> list:
        """Resources of `resource_type` shared with the current caller, as
        [{ownerId, groupId}] (groupId None = all of that owner's resources of
        that type). Resolved from grants with the trusted caller identity. Lazy
        imports keep the eval_mcp package DB-free and avoid a circular import
        with backend.api.main. Fails closed (returns [])."""
        try:
            from backend.api.main import db
            if db is None or not self.user_id:
                return []
            from backend.core import sharing
            scopes = await sharing.list_shared_scopes(db, self.user_id, resource_type)
            return [
                {"ownerId": s["ownerId"], "groupId": s["groupId"]}
                for s in scopes
            ]
        except Exception as e:
            self.logger.warning(f"shared-scope resolution failed: {e}")
            return []

    # Read tools that should receive shared_scopes, and the resource_type each
    # one reads. The backend injects the authorized scopes; the model can't.
    _SHARED_SCOPE_TOOLS = {
        "list_evaluations": "eval",
        "get_evaluation_details": "eval",
        "generate_report": "eval",
        "list_datasets": "dataset",
        "list_judges": "judge",
        "list_optimizations": "optimization",
        "get_optimization_details": "optimization",
        "list_documents": "document",
    }

    # Params the backend injects server-side and the model must never set.
    _INJECTED_PARAMS = ("user_id", "shared_scopes", "owner_id")

    def _filter_user_id_from_schema(self, schema: Dict[str, Any]) -> Dict[str, Any]:
        """Remove backend-injected params from a tool schema so the agent never
        sees (or tries to set) them."""
        import copy
        filtered = copy.deepcopy(schema)

        for param in self._INJECTED_PARAMS:
            if "properties" in filtered and param in filtered["properties"]:
                del filtered["properties"][param]
            if "required" in filtered and param in filtered["required"]:
                filtered["required"] = [r for r in filtered["required"] if r != param]

        return filtered

    async def list_tools(self, retry_on_empty: bool = True) -> List[Dict[str, Any]]:
        """List all available tools from all servers."""
        # Wait for any in-flight reconnect to finish before reading
        # self.sessions. Without this, the chat backend's cancel
        # handler — which schedules `reconnect_server('eval')` as a
        # background task for <200ms Stop response — races against
        # the next agent turn's list_tools: reconnect deletes
        # sessions['eval'] mid-loop, agent sees a half-state, and the
        # whole next message fails with "Sorry, I encountered an error".
        # The lock is held for nanoseconds outside actual reconnects;
        # this is just a sync point.
        async with self._reconnect_lock:
            pass

        if not self.sessions:
            raise RuntimeError("Not connected to MCP servers")

        all_tools = []
        failed_servers = []

        # Collect tools from shared servers
        for server_name, session in list(self.sessions.items()):
            try:
                result = await session.list_tools()

                # Convert Tool objects to dicts and tag with server name
                for tool in result.tools:
                    input_schema = tool.inputSchema

                    # Hide backend-injected params from the model. (The unified
                    # server registers as "eval"; the older split names are kept
                    # for backward compat.)
                    if server_name in ("eval", "synthetic-eval", "dataset") and input_schema:
                        input_schema = self._filter_user_id_from_schema(input_schema)

                    all_tools.append({
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": input_schema,
                        "_server": server_name,  # Track which server provides this tool
                    })
            except Exception as e:
                self.logger.warning(f"Failed to list tools from {server_name}: {e}")
                failed_servers.append(server_name)

        # If any server failed, try to reconnect and retry once
        if failed_servers and retry_on_empty:
            self.logger.info(f"Reconnecting to failed servers: {failed_servers}")
            for server_name in failed_servers:
                await self.reconnect_server(server_name)
            # Retry without recursion
            return await self.list_tools(retry_on_empty=False)

        return all_tools

    async def call_tool(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        """Call a tool on the appropriate MCP server with auto-reconnect.

        Args:
            tool_name: Name of the tool to call
            arguments: Tool arguments

        Returns:
            Tool result
        """
        if not self.sessions:
            raise RuntimeError("Not connected to MCP servers")

        # Find which server has this tool (with auto-reconnect on failure)
        tools = await self.list_tools(retry_on_empty=True)
        server_name = None
        for tool in tools:
            if tool["name"] == tool_name:
                server_name = tool["_server"]
                break

        if not server_name:
            # Tool not found - try reconnecting all servers and retry once
            self.logger.warning(f"Tool '{tool_name}' not found, reconnecting all servers...")
            for srv in list(self._server_configs.keys()):
                await self.reconnect_server(srv)

            # Retry tool lookup
            tools = await self.list_tools(retry_on_empty=False)
            for tool in tools:
                if tool["name"] == tool_name:
                    server_name = tool["_server"]
                    break

            if not server_name:
                raise RuntimeError(f"Tool '{tool_name}' not found on any server")

        # Auto-inject user_id for tools that need it
        if self.user_id:
            arguments = arguments or {}
            arguments["user_id"] = self.user_id

        # For read tools, also inject the resources SHARED with this caller (of
        # the type that tool reads), so shared results surface in chat. Computed
        # server-side from grants using the trusted caller identity (NEVER from
        # the model), so the model cannot widen its own access. The eval_mcp
        # tools stay DB-free — they just receive an authorized list of
        # {ownerId, groupId}.
        if self.user_id and tool_name in self._SHARED_SCOPE_TOOLS:
            arguments = arguments or {}
            arguments["shared_scopes"] = await self._shared_scopes(
                self._SHARED_SCOPE_TOOLS[tool_name]
            )

        # Strip any model-supplied owner_id — cross-user reads are authorized
        # only via the injected shared_scopes, never a raw owner the model names.
        if arguments and "owner_id" in arguments:
            del arguments["owner_id"]

        # Log tool call
        self.logger.info(
            json.dumps({
                "event": "tool_call_start",
                "server": server_name,
                "tool": tool_name,
                "arguments": arguments or {},
                "user_id": self.user_id
            })
        )

        # Try to call with one retry on connection failure
        max_retries = 2
        last_error = None

        for attempt in range(max_retries):
            try:
                session = self.sessions.get(server_name)
                if not session:
                    raise ConnectionError(f"No session for {server_name}")

                result = await session.call_tool(tool_name, arguments or {})

                # Log successful result
                self.logger.info(
                    json.dumps({
                        "event": "tool_call_success",
                        "server": server_name,
                        "tool": tool_name,
                        "result_preview": str(result)[:200] if result else None
                    })
                )

                return result

            except Exception as e:
                last_error = e
                error_str = str(e).lower()

                # Check if it's a connection error worth retrying
                is_connection_error = any(x in error_str for x in [
                    "connection", "closed", "reset", "refused", "timeout",
                    "eof", "broken pipe", "transport", "400", "bad request"
                ])

                if is_connection_error and attempt < max_retries - 1:
                    self.logger.warning(
                        json.dumps({
                            "event": "tool_call_retry",
                            "server": server_name,
                            "tool": tool_name,
                            "attempt": attempt + 1,
                            "error": str(e)
                        })
                    )
                    # Try to reconnect
                    if await self.reconnect_server(server_name):
                        continue  # Retry the call

                # Log error and give up
                self.logger.error(
                    json.dumps({
                        "event": "tool_call_error",
                        "server": server_name,
                        "tool": tool_name,
                        "error": str(e)
                    })
                )
                break

        raise RuntimeError(f"Tool call failed: {last_error}")

    async def list_resources(self) -> List[Dict[str, Any]]:
        """List available resources from all servers."""
        if not self.sessions:
            raise RuntimeError("Not connected to MCP servers")

        all_resources = []

        # Collect resources from each server
        for server_name, session in self.sessions.items():
            try:
                result = await session.list_resources()

                # Convert Resource objects to dicts and tag with server name
                for resource in result.resources:
                    all_resources.append({
                        "uri": resource.uri,
                        "name": resource.name,
                        "description": resource.description,
                        "mimeType": getattr(resource, "mimeType", None),
                        "_server": server_name,
                    })
            except Exception as e:
                self.logger.warning(f"Failed to list resources from {server_name}: {e}")

        return all_resources

    async def read_resource(self, uri: str) -> Any:
        """Read a resource by URI."""
        if not self.sessions:
            raise RuntimeError("Not connected to MCP servers")

        # Try to find which server has this resource
        resources = await self.list_resources()
        server_name = None
        for resource in resources:
            if resource["uri"] == uri:
                server_name = resource["_server"]
                break

        if not server_name:
            # Default to synthetic-eval server for backward compatibility
            server_name = "synthetic-eval"

        if server_name not in self.sessions:
            raise RuntimeError(f"Server '{server_name}' not connected")

        session = self.sessions[server_name]
        result = await session.read_resource(uri)
        return result




