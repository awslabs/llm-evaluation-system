"""Per-user promptfoo MCP server manager.

Spawns and manages separate promptfoo MCP server instances for each user,
ensuring evaluation results are stored in user-specific databases.
"""

import asyncio
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

import httpx

from backend.core.user_storage import get_user_promptfoo_dir

logger = logging.getLogger(__name__)


@dataclass
class PromptfooInstance:
    """Tracks a running promptfoo MCP server for a user."""
    port: int
    process: subprocess.Popen
    user_id: str
    started_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)


class PromptfooManager:
    """Manages per-user promptfoo MCP server instances.

    Each user gets their own promptfoo MCP server with PROMPTFOO_CONFIG_DIR
    pointing to their user directory, ensuring complete data isolation.
    """

    def __init__(
        self,
        base_port: int = 18001,  # Different range from viewers (15501+)
        idle_timeout_seconds: int = 3600,  # 1 hour (longer than viewer since evals take time)
        health_check_timeout: float = 30.0,
        health_check_interval: float = 0.5,
    ):
        self.base_port = base_port
        self.idle_timeout = idle_timeout_seconds
        self.health_check_timeout = health_check_timeout
        self.health_check_interval = health_check_interval

        self._instances: Dict[str, PromptfooInstance] = {}
        self._next_port = base_port
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the manager and background cleanup task."""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("PromptfooManager started")

    async def stop(self):
        """Stop all promptfoo instances and cleanup task."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Kill all instances
        for user_id in list(self._instances.keys()):
            await self._kill_instance(user_id)

        logger.info("PromptfooManager stopped")

    async def get_mcp_url(self, user_id: str) -> str:
        """Get the MCP server URL for a user, starting one if needed.

        Args:
            user_id: The user's ID

        Returns:
            The MCP URL (e.g., "http://localhost:18001/mcp")

        Raises:
            RuntimeError: If server fails to start
        """
        async with self._lock:
            # Check if instance exists and is running
            if user_id in self._instances:
                instance = self._instances[user_id]
                if instance.process.poll() is None:  # Still running
                    instance.last_accessed = time.time()
                    return f"http://localhost:{instance.port}/mcp"
                else:
                    # Process died, clean up
                    logger.warning(f"Promptfoo MCP for user {user_id} died, restarting")
                    del self._instances[user_id]

            # Start new instance
            port = self._allocate_port()
            instance = await self._start_instance(user_id, port)
            self._instances[user_id] = instance

            return f"http://localhost:{instance.port}/mcp"

    def get_instance_port(self, user_id: str) -> Optional[int]:
        """Get the port for a user's promptfoo instance if running."""
        if user_id not in self._instances:
            return None
        instance = self._instances[user_id]
        if instance.process.poll() is None:
            return instance.port
        return None

    def _allocate_port(self) -> int:
        """Allocate the next available port."""
        port = self._next_port
        self._next_port += 1
        return port

    async def _start_instance(self, user_id: str, port: int) -> PromptfooInstance:
        """Start a new promptfoo MCP server for a user."""
        user_dir = str(get_user_promptfoo_dir(user_id))

        # Ensure the directory exists
        os.makedirs(user_dir, exist_ok=True)

        logger.info(f"Starting promptfoo MCP for user {user_id} on port {port}, dir={user_dir}")

        # Set up environment with user-specific config dir
        env = os.environ.copy()
        env["PROMPTFOO_CONFIG_DIR"] = user_dir

        # Start promptfoo MCP server
        try:
            process = subprocess.Popen(
                ["promptfoo", "mcp", "--transport", "http", "--port", str(port)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            # Fall back to npx
            logger.info("promptfoo not found globally, using npx")
            process = subprocess.Popen(
                ["npx", "promptfoo@0.119.0", "mcp", "--transport", "http", "--port", str(port)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )

        instance = PromptfooInstance(
            port=port,
            process=process,
            user_id=user_id,
        )

        # Wait for server to become healthy
        healthy = await self._wait_for_health(port)
        if not healthy:
            process.kill()
            raise RuntimeError(f"Promptfoo MCP for user {user_id} failed to start")

        logger.info(f"Promptfoo MCP for user {user_id} is ready on port {port}")
        return instance

    async def _wait_for_health(self, port: int) -> bool:
        """Wait for MCP server to respond."""
        # The MCP server doesn't have a /health endpoint, so we check if the port is open
        # and try to connect to the /mcp endpoint
        url = f"http://localhost:{port}/mcp"
        start_time = time.time()

        async with httpx.AsyncClient() as client:
            while time.time() - start_time < self.health_check_timeout:
                try:
                    # Just check if we can connect - MCP uses SSE so we won't get a normal response
                    await client.get(url, timeout=2.0)
                    # Any response (even error) means server is up
                    return True
                except httpx.ConnectError:
                    pass
                except (httpx.RequestError, httpx.TimeoutException):
                    # Server responded but might not be ready - try again
                    pass

                await asyncio.sleep(self.health_check_interval)

        return False

    async def _kill_instance(self, user_id: str):
        """Kill a promptfoo instance."""
        if user_id not in self._instances:
            return

        instance = self._instances[user_id]

        if instance.process.poll() is None:
            logger.info(f"Killing promptfoo MCP for user {user_id} on port {instance.port}")
            instance.process.terminate()
            try:
                instance.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                instance.process.kill()

        del self._instances[user_id]

    async def _cleanup_loop(self):
        """Background task to cleanup idle instances."""
        while True:
            try:
                await asyncio.sleep(300)  # Check every 5 minutes
                await self._cleanup_idle_instances()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in promptfoo cleanup loop: {e}")

    async def _cleanup_idle_instances(self):
        """Remove instances that have been idle too long."""
        now = time.time()
        to_remove = []

        for user_id, instance in self._instances.items():
            idle_time = now - instance.last_accessed
            if idle_time > self.idle_timeout:
                to_remove.append(user_id)

        for user_id in to_remove:
            logger.info(f"Cleaning up idle promptfoo MCP for user {user_id}")
            await self._kill_instance(user_id)
