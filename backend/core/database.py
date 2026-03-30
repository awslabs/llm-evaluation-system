"""PostgreSQL database for chat sessions and messages.

Supports two authentication modes controlled by POSTGRES_USE_IAM_AUTH:
- IAM authentication (recommended): Uses short-lived IAM tokens for RDS
- Password authentication: Uses POSTGRES_PASSWORD environment variable
"""

import logging
import os
import re
import time
from datetime import datetime
from typing import List, Dict, Any, Optional

import asyncpg
import boto3

# Valid PostgreSQL identifier pattern (letters, numbers, underscores, starting with letter/underscore)
_VALID_DB_NAME_PATTERN = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')

logger = logging.getLogger(__name__)


class DatabaseError(Exception):
    """Raised when a database operation fails."""
    pass


def _require_env(name: str) -> str:
    """Get a required environment variable or raise immediately."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value


class Database:
    # IAM tokens are valid for 15 minutes, refresh at 10 minutes to be safe
    TOKEN_REFRESH_SECONDS = 600

    def __init__(self):
        # Get connection parameters from environment (all required)
        self.host = _require_env("POSTGRES_HOST")
        self.port = int(os.getenv("POSTGRES_PORT", "5432"))
        self.database = self._validate_db_name(_require_env("POSTGRES_DB"))
        self.user = _require_env("POSTGRES_USER")
        self.use_iam_auth = os.getenv("POSTGRES_USE_IAM_AUTH", "").lower() == "true"
        self.region = os.getenv("AWS_REGION", "us-west-2")

        # For IAM auth, track token generation time
        self._token_generated_at: float = 0
        self._pool: Optional[asyncpg.Pool] = None

    async def initialize(self):
        """Create connection pool and initialize schema. Must be called after __init__."""
        await self._create_pool()
        await self.init_db()

    @staticmethod
    def _validate_db_name(name: str) -> str:
        """Validate database name is a safe PostgreSQL identifier."""
        if not name:
            raise ValueError("Database name cannot be empty")
        if not _VALID_DB_NAME_PATTERN.match(name):
            raise ValueError(
                f"Invalid database name '{name}'. "
                "Must start with letter/underscore and contain only letters, numbers, underscores."
            )
        if len(name) > 63:
            raise ValueError(f"Database name '{name}' exceeds PostgreSQL 63-character limit")
        return name

    def _get_iam_token(self) -> str:
        """Generate an IAM authentication token for RDS."""
        client = boto3.client("rds", region_name=self.region)
        token = client.generate_db_auth_token(
            DBHostname=self.host,
            Port=self.port,
            DBUsername=self.user,
            Region=self.region,
        )
        return token

    def _get_password(self) -> str:
        """Get the password for the connection (IAM token or env var)."""
        if self.use_iam_auth:
            return self._get_iam_token()
        return os.getenv("POSTGRES_PASSWORD", "")

    async def _create_pool(self):
        """Create or recreate the connection pool."""
        if self._pool:
            try:
                await self._pool.close()
            except Exception as e:
                logger.warning(f"Failed to close existing connection pool: {e}")

        connect_kwargs = {}
        if self.use_iam_auth:
            connect_kwargs["ssl"] = "require"

        self._pool = await asyncpg.create_pool(
            host=self.host,
            port=self.port,
            database=self.database,
            user=self.user,
            password=self._get_password(),
            min_size=1,
            max_size=20,
            **connect_kwargs,
        )
        self._token_generated_at = time.time()

    async def _ensure_pool_fresh(self):
        """Ensure the connection pool has a fresh IAM token."""
        if not self.use_iam_auth:
            return  # No token refresh needed for password auth

        elapsed = time.time() - self._token_generated_at
        if elapsed >= self.TOKEN_REFRESH_SECONDS:
            logger.info("Refreshing database connection pool (IAM token expiring)")
            await self._create_pool()

    async def init_db(self):
        """Initialize database schema."""
        async with self._pool.acquire() as conn:
            # Users table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)

            # Chat sessions table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
            """)

            # Messages table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp TIMESTAMP NOT NULL DEFAULT NOW(),
                    FOREIGN KEY (session_id) REFERENCES chat_sessions (id)
                )
            """)

            # Create indexes for better performance
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sessions_user_id
                ON chat_sessions(user_id)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_session_id
                ON messages(session_id)
            """)

    async def create_user(self, user_id: str, username: str) -> None:
        """Create a new user.

        Raises:
            DatabaseError: If the user cannot be created.
        """
        await self._ensure_pool_fresh()
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO users (id, username, created_at) VALUES ($1, $2, $3) ON CONFLICT (id) DO NOTHING",
                    user_id, username, datetime.now(),
                )
        except asyncpg.PostgresError as e:
            raise DatabaseError(f"Failed to create user {user_id}: {e}") from e

    async def create_session(self, session_id: str, user_id: str, title: str = "New Chat") -> None:
        """Create a new chat session.

        Raises:
            DatabaseError: If the session cannot be created.
        """
        await self._ensure_pool_fresh()
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO chat_sessions (id, user_id, title, created_at) VALUES ($1, $2, $3, $4) ON CONFLICT (id) DO NOTHING",
                    session_id, user_id, title, datetime.now(),
                )
        except asyncpg.PostgresError as e:
            raise DatabaseError(f"Failed to create session {session_id}: {e}") from e

    async def get_user_sessions(self, user_id: str) -> List[Dict[str, Any]]:
        """Get all chat sessions for a user."""
        await self._ensure_pool_fresh()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, title, created_at FROM chat_sessions WHERE user_id = $1 ORDER BY created_at DESC",
                user_id,
            )

            sessions = []
            for row in rows:
                messages = await self.get_session_messages(row["id"])
                sessions.append({
                    "id": row["id"],
                    "title": row["title"],
                    "createdAt": row["created_at"].isoformat(),
                    "messages": messages,
                })
            return sessions

    async def get_session_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Get all messages for a session."""
        await self._ensure_pool_fresh()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, role, content, timestamp FROM messages WHERE session_id = $1 ORDER BY timestamp ASC",
                session_id,
            )

            return [
                {
                    "id": row["id"],
                    "role": row["role"],
                    "content": row["content"],
                    "timestamp": row["timestamp"].isoformat(),
                }
                for row in rows
            ]

    async def save_message(
        self, message_id: str, session_id: str, role: str, content: str
    ) -> None:
        """Save a message to a session.

        Raises:
            DatabaseError: If the message cannot be saved.
        """
        await self._ensure_pool_fresh()
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES ($1, $2, $3, $4, $5)",
                    message_id, session_id, role, content, datetime.now(),
                )
        except asyncpg.PostgresError as e:
            raise DatabaseError(f"Failed to save message {message_id}: {e}") from e

    async def update_session_title(self, session_id: str, title: str) -> None:
        """Update a session's title.

        Raises:
            DatabaseError: If the title cannot be updated.
        """
        await self._ensure_pool_fresh()
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE chat_sessions SET title = $1 WHERE id = $2",
                    title, session_id,
                )
        except asyncpg.PostgresError as e:
            raise DatabaseError(f"Failed to update session title {session_id}: {e}") from e

    async def close(self):
        """Close all connections in the pool."""
        if self._pool:
            await self._pool.close()
