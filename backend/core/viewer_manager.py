"""Per-user promptfoo viewer process manager.

Spawns and manages separate promptfoo viewer instances for each user,
providing data isolation in a multi-tenant environment.

Viewers are pre-warmed on login and kept alive for 48 hours from last login.
Each login resets the 48-hour timer.
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

# 48 hours in seconds
DEFAULT_LOGIN_TIMEOUT = 48 * 60 * 60


@dataclass
class ViewerInstance:
    """Tracks a running viewer instance for a user."""
    port: int
    process: subprocess.Popen
    user_id: str
    started_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    last_login: float = field(default_factory=time.time)  # Reset on each login, controls expiry


class ViewerManager:
    """Manages per-user promptfoo viewer processes.

    Each user gets their own viewer instance pointing to their
    PROMPTFOO_CONFIG_DIR, ensuring data isolation.

    Viewers are pre-warmed on login (non-blocking) and kept alive for
    48 hours from last login. Each login resets the timer.
    """

    def __init__(
        self,
        base_port: int = 15501,
        login_timeout_seconds: int = DEFAULT_LOGIN_TIMEOUT,  # 48 hours from last login
        health_check_timeout: float = 60.0,
        health_check_interval: float = 0.5,
    ):
        self.base_port = base_port
        self.login_timeout = login_timeout_seconds
        self.health_check_timeout = health_check_timeout
        self.health_check_interval = health_check_interval

        self._viewers: Dict[str, ViewerInstance] = {}
        self._next_port = base_port
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._spawning: set[str] = set()  # Track users currently being spawned

    async def start(self):
        """Start the manager and background cleanup task."""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("ViewerManager started")

    async def stop(self):
        """Stop all viewer processes and cleanup task."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Kill all viewer processes
        for user_id, viewer in list(self._viewers.items()):
            await self._kill_viewer(user_id)

        logger.info("ViewerManager stopped")

    async def on_user_login(self, user_id: str) -> None:
        """Called when a user logs in. Pre-warms viewer in background.

        If viewer exists, refreshes the 48-hour login timer.
        If viewer doesn't exist, spawns one in background (non-blocking).

        Args:
            user_id: The user's ID
        """
        async with self._lock:
            if user_id in self._viewers:
                viewer = self._viewers[user_id]
                if viewer.process.poll() is None:  # Still running
                    viewer.last_login = time.time()
                    logger.info(f"Refreshed login timer for user {user_id} viewer")
                    return
                else:
                    # Process died, clean up
                    del self._viewers[user_id]

            # Don't spawn if already spawning
            if user_id in self._spawning:
                return

            self._spawning.add(user_id)

        # Spawn in background (non-blocking)
        asyncio.create_task(self._spawn_viewer_background(user_id))

    async def _spawn_viewer_background(self, user_id: str) -> None:
        """Spawn a viewer in the background. Non-blocking."""
        try:
            async with self._lock:
                # Double-check not already created while waiting for lock
                if user_id in self._viewers:
                    return

                port = self._allocate_port()

            # Start viewer outside lock to avoid blocking other operations
            viewer = await self._start_viewer(user_id, port)

            async with self._lock:
                self._viewers[user_id] = viewer
                logger.info(f"Pre-warmed viewer for user {user_id} on port {port}")

        except Exception as e:
            logger.error(f"Failed to pre-warm viewer for user {user_id}: {e}")
        finally:
            self._spawning.discard(user_id)

    async def _check_viewer_health(self, port: int) -> bool:
        """Quick health check for a viewer (2s timeout)."""
        url = f"http://localhost:{port}/health"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=2.0)
                return response.status_code == 200
        except (httpx.RequestError, httpx.TimeoutException):
            return False

    async def get_viewer_url(self, user_id: str) -> str:
        """Get the viewer URL for a user, starting one if needed.

        Args:
            user_id: The user's ID

        Returns:
            The URL to access the user's viewer (e.g., "http://localhost:15501")

        Raises:
            RuntimeError: If viewer fails to start or become healthy
        """
        async with self._lock:
            # Check if viewer already exists and is running
            if user_id in self._viewers:
                viewer = self._viewers[user_id]
                if viewer.process.poll() is None:  # Process alive
                    # Verify it's actually responding (not hung)
                    if await self._check_viewer_health(viewer.port):
                        viewer.last_accessed = time.time()
                        return f"http://localhost:{viewer.port}"
                    else:
                        # Process alive but not responding - kill it
                        logger.warning(f"Viewer for user {user_id} hung (not responding to health check), killing")
                        viewer.process.kill()
                        try:
                            viewer.process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            pass
                        del self._viewers[user_id]
                else:
                    # Process died, clean up
                    logger.warning(f"Viewer for user {user_id} died, restarting")
                    del self._viewers[user_id]

            # Remove from spawning set if it was there (spawn failed or timed out)
            self._spawning.discard(user_id)

            # Start new viewer
            port = self._allocate_port()
            viewer = await self._start_viewer(user_id, port)
            self._viewers[user_id] = viewer

            return f"http://localhost:{viewer.port}"

    def get_viewer_status(self, user_id: str) -> Optional[dict]:
        """Get status of a user's viewer if running."""
        if user_id not in self._viewers:
            return None

        viewer = self._viewers[user_id]
        is_running = viewer.process.poll() is None
        now = time.time()

        return {
            "port": viewer.port,
            "running": is_running,
            "started_at": viewer.started_at,
            "last_accessed": viewer.last_accessed,
            "last_login": viewer.last_login,
            "uptime_seconds": now - viewer.started_at,
            "idle_seconds": now - viewer.last_accessed,
            "expires_in_seconds": self.login_timeout - (now - viewer.last_login),
        }

    def _allocate_port(self) -> int:
        """Allocate the next available port."""
        port = self._next_port
        self._next_port += 1
        return port

    async def _start_viewer(self, user_id: str, port: int) -> ViewerInstance:
        """Start a new viewer process for a user."""
        user_dir = str(get_user_promptfoo_dir(user_id))

        # Ensure the directory exists
        os.makedirs(user_dir, exist_ok=True)


        logger.info(f"Starting viewer for user {user_id} on port {port}, dir={user_dir}")

        # Start promptfoo viewer process
        # Use 'promptfoo' directly (assumes global install) for faster startup
        # Fall back to 'npx promptfoo' if not available
        env = os.environ.copy()
        env["PROMPTFOO_CONFIG_DIR"] = user_dir
        env["PROMPTFOO_DISABLE_UPDATE"] = "true"  # Hide update banner in self-hosted

        try:
            process = subprocess.Popen(
                ["promptfoo", "view", "--port", str(port), "--no"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # Don't inherit stdin to avoid blocking
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            # Fall back to npx
            logger.info("promptfoo not found globally, using npx")
            process = subprocess.Popen(
                ["npx", "promptfoo", "view", "--port", str(port), "--no"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )

        viewer = ViewerInstance(
            port=port,
            process=process,
            user_id=user_id,
        )

        # Wait for viewer to become healthy
        healthy = await self._wait_for_health(port)
        if not healthy:
            process.kill()
            raise RuntimeError(f"Viewer for user {user_id} failed to start")

        logger.info(f"Viewer for user {user_id} is ready on port {port}")
        return viewer

    async def _wait_for_health(self, port: int) -> bool:
        """Wait for viewer to respond to health check."""
        url = f"http://localhost:{port}/health"
        start_time = time.time()

        async with httpx.AsyncClient() as client:
            while time.time() - start_time < self.health_check_timeout:
                try:
                    response = await client.get(url, timeout=2.0)
                    if response.status_code == 200:
                        return True
                except (httpx.RequestError, httpx.TimeoutException):
                    pass

                await asyncio.sleep(self.health_check_interval)

        return False

    async def _kill_viewer(self, user_id: str):
        """Kill a viewer process."""
        if user_id not in self._viewers:
            return

        viewer = self._viewers[user_id]

        if viewer.process.poll() is None:
            logger.info(f"Killing viewer for user {user_id} on port {viewer.port}")
            viewer.process.terminate()
            try:
                viewer.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                viewer.process.kill()

        del self._viewers[user_id]

    async def _cleanup_loop(self):
        """Background task to cleanup expired viewers."""
        while True:
            try:
                await asyncio.sleep(300)  # Check every 5 minutes (no rush for 48h timeout)
                await self._cleanup_expired_viewers()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in viewer cleanup loop: {e}")

    async def _cleanup_expired_viewers(self):
        """Remove viewers that have exceeded 48 hours since last login."""
        now = time.time()
        to_remove = []

        for user_id, viewer in self._viewers.items():
            time_since_login = now - viewer.last_login
            if time_since_login > self.login_timeout:
                to_remove.append(user_id)

        for user_id in to_remove:
            logger.info(f"Cleaning up expired viewer for user {user_id} (48h since last login)")
            await self._kill_viewer(user_id)
