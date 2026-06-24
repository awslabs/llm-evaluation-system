"""PostgreSQL database for chat sessions and messages.

Supports two authentication modes controlled by POSTGRES_USE_IAM_AUTH:
- IAM authentication (recommended): Uses short-lived IAM tokens for RDS
- Password authentication: Uses POSTGRES_PASSWORD environment variable
"""

import asyncio
import hashlib
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
        # Serializes pool refresh so concurrent requests don't observe
        # a half-closed pool. Without this lock, two coroutines arriving
        # at _ensure_pool_fresh() simultaneously both call _create_pool()
        # → A closes the old pool and starts opening a new one (~100ms-2s)
        # → during that window, self._pool references a closed pool
        # → any other request hitting self._pool.acquire() crashes with
        #   InterfaceError: pool is closed. This was visible in EKS logs
        #   as a chat_stream → db.create_user traceback every ~10 minutes
        #   (IAM token TTL) plus more often under stress.
        self._pool_lock: asyncio.Lock = asyncio.Lock()
        # Set during graceful shutdown so we stop trying to use the
        # pool (close() can't be safely re-opened — process is exiting).
        self._closed: bool = False

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
        """Create or recreate the connection pool.

        IMPORTANT: callers MUST hold ``self._pool_lock`` to serialize
        recreation, otherwise concurrent callers double-close and race
        on assigning self._pool. The two callers in this class do:
          - ``initialize()`` at startup — single-threaded, no race
          - ``_ensure_pool_fresh()`` — acquires the lock itself
        Build the new pool FIRST, then swap self._pool atomically, then
        close the old one. That way self._pool never references a closed
        pool, even if some weird code path doesn't go through the
        _ensure_pool_fresh guard.
        """
        connect_kwargs = {}
        if self.use_iam_auth:
            connect_kwargs["ssl"] = "require"

        new_pool = await asyncpg.create_pool(
            host=self.host,
            port=self.port,
            database=self.database,
            user=self.user,
            password=self._get_password(),
            min_size=1,
            max_size=20,
            **connect_kwargs,
        )

        old_pool = self._pool
        self._pool = new_pool
        self._token_generated_at = time.time()

        if old_pool is not None:
            try:
                await old_pool.close()
            except Exception as e:
                logger.warning(f"Failed to close previous connection pool: {e}")

    def _pool_is_closed(self) -> bool:
        """Detect a closed/missing pool without triggering an exception.
        asyncpg's pool exposes `_closed` and `_initialized` internals;
        fall back conservatively if either's not present.
        """
        p = self._pool
        if p is None:
            return True
        # asyncpg.Pool._closed is True once close() finished; _closing
        # is set during graceful close. Either means we can't acquire.
        if getattr(p, "_closed", False) or getattr(p, "_closing", False):
            return True
        return False

    async def _ensure_pool_fresh(self):
        """Ensure the connection pool has a fresh IAM token AND that
        the pool object isn't a stale closed reference.

        Two conditions trigger recreation:
        - IAM token is older than TOKEN_REFRESH_SECONDS (normal case)
        - The current self._pool is closed (rolling deploy case: a
          terminating pod's lifespan shutdown ran `db.close()` while
          an in-flight request was still being processed; without
          this, the request hits self._pool.acquire() → InterfaceError)

        Serialized via lock so concurrent callers don't race.
        """
        if self._closed:
            # Process is exiting — don't try to reopen the pool; the
            # caller's request will get a clean DatabaseError.
            raise DatabaseError("Database is shutting down")

        if not self.use_iam_auth and not self._pool_is_closed():
            return

        # Cheap pre-check: avoid lock contention in the common case
        # where the token is fresh AND the pool isn't closed.
        elapsed = time.time() - self._token_generated_at
        if elapsed < self.TOKEN_REFRESH_SECONDS and not self._pool_is_closed():
            return

        async with self._pool_lock:
            if self._closed:
                raise DatabaseError("Database is shutting down")
            # Re-check under the lock — another coroutine may have
            # already refreshed during the wait.
            elapsed = time.time() - self._token_generated_at
            needs_token_refresh = (
                self.use_iam_auth and elapsed >= self.TOKEN_REFRESH_SECONDS
            )
            needs_pool_rebuild = self._pool_is_closed()
            if not needs_token_refresh and not needs_pool_rebuild:
                return
            if needs_pool_rebuild:
                logger.warning(
                    "Rebuilding database connection pool — previous pool was closed "
                    "(likely a rolling-deploy SIGTERM race)"
                )
            else:
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
            # email is added idempotently (older deployments predate it). It
            # backs user discovery for the share-by-email picker — grants are
            # still keyed on the id (X-Forwarded-User), email is only a lookup.
            await conn.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)"
            )

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

            # Session cancellations table — used to propagate Stop
            # across pods. Backend runs as a multi-pod Deployment;
            # the chat request may land on pod A while the cancel
            # HTTP arrives at pod B (ALB lb_cookie stickiness is
            # configured but not reliably preserved through
            # CloudFront → ALB → browser → ALB hops in practice).
            # A simple row in this table is the cross-pod signal:
            # cancel_chat writes it, the agent's per-iteration poll
            # in run_agent_background reads it.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS session_cancellations (
                    session_id TEXT PRIMARY KEY,
                    cancelled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    eval_info TEXT
                )
            """)

            # Cross-pod "session is running" signal. Backend runs as a
            # multi-pod Deployment; chat_status checks in-memory first
            # and falls back to this table so a tab that reconnects on a
            # different pod can still tell the task is alive. Mirrors the
            # session_cancellations pattern. Row written at task start,
            # deleted in the finally block of run_agent_background.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS session_active (
                    session_id TEXT PRIMARY KEY,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    pod_id TEXT
                )
            """)

            # Teams — a team is just another principal that grants can
            # target. Membership lives in team_members. See
            # docs/EVAL_SHARING_DESIGN.md.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS teams (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    FOREIGN KEY (created_by) REFERENCES users (id)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS team_members (
                    team_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'member'
                        CHECK (role IN ('admin', 'member')),
                    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (team_id, user_id),
                    FOREIGN KEY (team_id) REFERENCES teams (id),
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
            """)

            # Sharing grants. One row expresses every sharing case:
            # share-one (group_id set) or share-all (group_id NULL), to a
            # user/team/org principal, for any RESOURCE TYPE (eval, dataset,
            # judge, optimization, document). Authorization is resolved against
            # (resource_type, owner_id, group_id) — group_id alone is NOT
            # globally unique (it's an eval run_id / dataset id / judge id /
            # doc path), so owner_id+resource_type supply the scope. We do NOT
            # FK principal_id/group_id: a grantee may not have logged in yet
            # (users are created lazily) and group_id is not a DB row.
            # resource_type/principal_type/role are CHECK-constrained so an
            # unknown value can't silently fail open. Deny-by-default makes a
            # dangling principal harmless. See docs/EVAL_SHARING_DESIGN.md.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS eval_grants (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    group_id TEXT,
                    principal_type TEXT NOT NULL
                        CHECK (principal_type IN ('user', 'team', 'org')),
                    principal_id TEXT,
                    role TEXT NOT NULL DEFAULT 'viewer'
                        CHECK (role IN ('viewer', 'editor', 'owner')),
                    granted_by TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    FOREIGN KEY (owner_id) REFERENCES users (id),
                    FOREIGN KEY (granted_by) REFERENCES users (id),
                    UNIQUE (owner_id, group_id, principal_type, principal_id)
                )
            """)
            # resource_type added idempotently — rows predating multi-resource
            # sharing are evals. The deterministic id PK (see _grant_id) is what
            # dedupes now, so the old 4-col UNIQUE (which omits resource_type)
            # is left in place harmlessly; a dataset and an eval sharing the same
            # group_id string would collide on it, so we drop it if present.
            await conn.execute(
                "ALTER TABLE eval_grants ADD COLUMN IF NOT EXISTS "
                "resource_type TEXT NOT NULL DEFAULT 'eval'"
            )
            await conn.execute(
                "ALTER TABLE eval_grants DROP CONSTRAINT IF EXISTS "
                "eval_grants_owner_id_group_id_principal_type_principal_id_key"
            )
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_grants_principal
                ON eval_grants(principal_type, principal_id)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_grants_owner
                ON eval_grants(owner_id)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_team_members_user
                ON team_members(user_id)
            """)

    async def create_user(
        self, user_id: str, username: str, email: Optional[str] = None
    ) -> None:
        """Create a user (idempotent). If `email` is provided, it is recorded /
        refreshed so the share-by-email picker can resolve it to this id.

        Raises:
            DatabaseError: If the user cannot be created.
        """
        await self._ensure_pool_fresh()
        try:
            async with self._pool.acquire() as conn:
                # COALESCE keeps an existing email if this call passes none,
                # and updates it when a fresh one arrives on a later login.
                await conn.execute(
                    """
                    INSERT INTO users (id, username, email, created_at)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (id) DO UPDATE
                      SET email = COALESCE(EXCLUDED.email, users.email)
                    """,
                    user_id, username, email, datetime.now(),
                )
        except asyncpg.PostgresError as e:
            raise DatabaseError(f"Failed to create user {user_id}: {e}") from e

    async def search_users(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Find users by id or email substring (case-insensitive), for the
        share recipient picker. Returns [{id, email}]. Empty query → []."""
        q = (query or "").strip()
        if not q:
            return []
        await self._ensure_pool_fresh()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, email FROM users
                WHERE id ILIKE '%' || $1 || '%' OR email ILIKE '%' || $1 || '%'
                ORDER BY id LIMIT $2
                """,
                q, limit,
            )
            return [{"id": r["id"], "email": r["email"]} for r in rows]

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

    async def mark_session_cancelled(self, session_id: str, eval_info_json: str = "") -> None:
        """Mark a chat session as cancelled. Picked up cross-pod by the
        agent loop's poll in run_agent_background. UPSERT so a repeat
        cancel within the same chat turn just refreshes the timestamp.
        """
        await self._ensure_pool_fresh()
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO session_cancellations (session_id, cancelled_at, eval_info)
                    VALUES ($1, NOW(), $2)
                    ON CONFLICT (session_id) DO UPDATE
                      SET cancelled_at = NOW(), eval_info = EXCLUDED.eval_info
                    """,
                    session_id, eval_info_json,
                )
        except asyncpg.PostgresError as e:
            raise DatabaseError(f"Failed to mark session {session_id} cancelled: {e}") from e

    async def clear_session_cancellation(self, session_id: str) -> None:
        """Clear the cancellation flag for a session. Called when a new
        chat turn STARTS so a fresh user message doesn't immediately
        see itself as already-cancelled from a previous Stop.
        """
        await self._ensure_pool_fresh()
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM session_cancellations WHERE session_id = $1",
                    session_id,
                )
        except asyncpg.PostgresError as e:
            # Non-fatal — worst case the next iteration immediately
            # sees the stale flag and cancels itself. Just log.
            logger.warning(f"Failed to clear cancellation for {session_id}: {e}")

    async def get_session_cancellation(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Return {cancelled_at, eval_info} if this session is marked
        cancelled, else None. Used by the agent loop's per-iteration
        cross-pod cancel check.
        """
        await self._ensure_pool_fresh()
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT cancelled_at, eval_info FROM session_cancellations WHERE session_id = $1",
                    session_id,
                )
                if row is None:
                    return None
                return {
                    "cancelled_at": row["cancelled_at"],
                    "eval_info": row["eval_info"],
                }
        except asyncpg.PostgresError as e:
            # Don't crash the agent loop on a transient DB hiccup —
            # the worst case is the user clicks Stop again and the
            # next poll catches it.
            logger.warning(f"Failed to check cancellation for {session_id}: {e}")
            return None

    # ------------------------------------------------------------------
    # Cross-pod session-active signals (mirrors session_cancellations).
    # ------------------------------------------------------------------

    async def mark_session_active(self, session_id: str, pod_id: str = "") -> None:
        """Record that a chat session is running on this pod.

        Written at task start so a tab that reconnects on a different pod
        can still learn the session is live via get_session_active().
        UPSERT so a retry within the same turn just refreshes started_at.
        """
        await self._ensure_pool_fresh()
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO session_active (session_id, started_at, pod_id)
                    VALUES ($1, NOW(), $2)
                    ON CONFLICT (session_id) DO UPDATE
                      SET started_at = NOW(), pod_id = EXCLUDED.pod_id
                    """,
                    session_id, pod_id,
                )
        except asyncpg.PostgresError as e:
            # Non-fatal — worst case the cross-pod status check misses
            # this session and the UI treats it as idle. Log and move on.
            logger.warning(f"Failed to mark session {session_id} active: {e}")

    async def clear_session_active(self, session_id: str) -> None:
        """Remove the active flag when the session finishes or is cancelled."""
        await self._ensure_pool_fresh()
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM session_active WHERE session_id = $1",
                    session_id,
                )
        except asyncpg.PostgresError as e:
            logger.warning(f"Failed to clear session_active for {session_id}: {e}")

    async def get_session_active(self, session_id: str) -> bool:
        """Return True if the session has an active row in session_active.

        Used by chat_status as the cross-pod fallback when the session
        is not found in the local in-memory active_tasks dict.
        """
        await self._ensure_pool_fresh()
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT 1 FROM session_active WHERE session_id = $1",
                    session_id,
                )
                return row is not None
        except asyncpg.PostgresError as e:
            # On DB error default to False (idle) — better to briefly
            # show idle than to hang the UI polling forever.
            logger.warning(f"Failed to check session_active for {session_id}: {e}")
            return False

    # ------------------------------------------------------------------
    # Eval sharing: grants + teams. See docs/EVAL_SHARING_DESIGN.md.
    # ------------------------------------------------------------------

    @staticmethod
    def _grant_id(owner_id: str, group_id: Optional[str],
                  principal_type: str, principal_id: Optional[str],
                  resource_type: str = "eval") -> str:
        """Deterministic id for a grant tuple.

        Used as the PRIMARY KEY so ON CONFLICT dedupes idempotently. The
        composite UNIQUE constraint alone can't, because Postgres treats
        NULLs (share-all group_id, org principal_id) as distinct — so two
        identical share-all grants would otherwise both insert. Hashing the
        normalized tuple gives a stable id that collides on a true duplicate.
        resource_type is part of the key so an eval and a dataset that happen
        to share a group_id string get distinct grant rows. NOTE: eval grants
        keep the legacy 'eval'-free key (resource_type omitted from the hash)
        so ids stay stable across the multi-resource migration.
        """
        parts = [owner_id, group_id or "", principal_type, principal_id or ""]
        if resource_type != "eval":
            parts.append(resource_type)
        key = "\x1f".join(parts)
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]

    async def add_grant(
        self,
        owner_id: str,
        group_id: Optional[str],
        principal_type: str,
        principal_id: Optional[str],
        granted_by: str,
        role: str = "viewer",
        resource_type: str = "eval",
    ) -> str:
        """Create a sharing grant. Returns the grant id.

        group_id=None means "all of owner's <resource_type>s" (incl. future).
        principal_type='org' means everyone; principal_id is then ignored.
        resource_type is one of eval/dataset/judge/optimization/document.
        Idempotent via the deterministic id.

        Raises:
            DatabaseError: If the grant cannot be created.
        """
        grant_id = self._grant_id(
            owner_id, group_id, principal_type, principal_id, resource_type
        )
        await self._ensure_pool_fresh()
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO eval_grants
                        (id, owner_id, group_id, resource_type, principal_type,
                         principal_id, role, granted_by, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
                    ON CONFLICT (id) DO NOTHING
                    """,
                    grant_id, owner_id, group_id, resource_type, principal_type,
                    principal_id, role, granted_by,
                )
        except asyncpg.PostgresError as e:
            raise DatabaseError(f"Failed to add grant {grant_id}: {e}") from e
        logger.info(
            f"[GRANT] {granted_by} granted {principal_type}:{principal_id} "
            f"{role} on {resource_type} owner={owner_id} group={group_id or '*'}"
        )
        return grant_id

    async def remove_grant(self, grant_id: str, owner_id: str) -> bool:
        """Revoke a grant. owner_id is required so a caller can only delete
        grants on their OWN evals (defense in depth — the route also checks).
        Returns True if a row was deleted.

        Raises:
            DatabaseError: If the delete fails.
        """
        await self._ensure_pool_fresh()
        try:
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    "DELETE FROM eval_grants WHERE id = $1 AND owner_id = $2",
                    grant_id, owner_id,
                )
        except asyncpg.PostgresError as e:
            raise DatabaseError(f"Failed to remove grant {grant_id}: {e}") from e
        deleted = result.endswith(" 1")
        if deleted:
            logger.info(f"[GRANT] {owner_id} revoked grant {grant_id}")
        return deleted

    async def list_grants_by_owner(self, owner_id: str) -> List[Dict[str, Any]]:
        """List grants the owner has created (for the 'who can see my evals' UI)."""
        await self._ensure_pool_fresh()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, group_id, resource_type, principal_type,
                       principal_id, role, created_at
                FROM eval_grants WHERE owner_id = $1 ORDER BY created_at DESC
                """,
                owner_id,
            )
            return [
                {
                    "id": row["id"],
                    "groupId": row["group_id"],
                    "resourceType": row["resource_type"],
                    "principalType": row["principal_type"],
                    "principalId": row["principal_id"],
                    "role": row["role"],
                    "createdAt": row["created_at"].isoformat(),
                }
                for row in rows
            ]

    async def list_grants_for_principals(
        self, principals: List[tuple]
    ) -> List[Dict[str, Any]]:
        """Return all grants visible to a caller given their resolved
        principals — a list of (principal_type, principal_id) tuples, e.g.
        [('user', caller), ('team', t1), ('org', None)].

        This is the read side of the resolver: it yields the (owner_id,
        group_id) scopes the caller may read. group_id NULL = all of that
        owner's evals.
        """
        if not principals:
            return []
        await self._ensure_pool_fresh()
        # Build an OR of (principal_type, principal_id) pairs. principal_id
        # may be NULL (org), so compare with IS NOT DISTINCT FROM.
        clauses = []
        args: List[Any] = []
        for ptype, pid in principals:
            args.append(ptype)
            args.append(pid)
            n = len(args)
            clauses.append(
                f"(principal_type = ${n - 1} "
                f"AND principal_id IS NOT DISTINCT FROM ${n})"
            )
        where = " OR ".join(clauses)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT owner_id, group_id, resource_type, role
                FROM eval_grants
                WHERE {where}
                """,
                *args,
            )
            return [
                {
                    "ownerId": row["owner_id"],
                    "groupId": row["group_id"],
                    "resourceType": row["resource_type"],
                    "role": row["role"],
                }
                for row in rows
            ]

    async def get_teams_for_user(self, user_id: str) -> List[str]:
        """Return the team ids a user belongs to (for principal resolution)."""
        await self._ensure_pool_fresh()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT team_id FROM team_members WHERE user_id = $1",
                user_id,
            )
            return [row["team_id"] for row in rows]

    async def create_team(self, team_id: str, name: str, created_by: str) -> None:
        """Create a team and add the creator as an admin member.

        Raises:
            DatabaseError: If the team cannot be created.
        """
        await self._ensure_pool_fresh()
        try:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO teams (id, name, created_by, created_at)
                        VALUES ($1, $2, $3, NOW())
                        ON CONFLICT (id) DO NOTHING
                        """,
                        team_id, name, created_by,
                    )
                    await conn.execute(
                        """
                        INSERT INTO team_members (team_id, user_id, role, added_at)
                        VALUES ($1, $2, 'admin', NOW())
                        ON CONFLICT (team_id, user_id) DO NOTHING
                        """,
                        team_id, created_by,
                    )
        except asyncpg.PostgresError as e:
            raise DatabaseError(f"Failed to create team {team_id}: {e}") from e

    async def add_team_member(
        self, team_id: str, user_id: str, role: str = "member"
    ) -> None:
        """Add a user to a team.

        Raises:
            DatabaseError: If the member cannot be added.
        """
        await self._ensure_pool_fresh()
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO team_members (team_id, user_id, role, added_at)
                    VALUES ($1, $2, $3, NOW())
                    ON CONFLICT (team_id, user_id) DO UPDATE SET role = EXCLUDED.role
                    """,
                    team_id, user_id, role,
                )
        except asyncpg.PostgresError as e:
            raise DatabaseError(
                f"Failed to add member {user_id} to team {team_id}: {e}"
            ) from e

    async def remove_team_member(self, team_id: str, user_id: str) -> bool:
        """Remove a member from a team. Returns True if a row was deleted.

        Raises:
            DatabaseError: If the delete fails.
        """
        await self._ensure_pool_fresh()
        try:
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    "DELETE FROM team_members WHERE team_id = $1 AND user_id = $2",
                    team_id, user_id,
                )
        except asyncpg.PostgresError as e:
            raise DatabaseError(
                f"Failed to remove member {user_id} from team {team_id}: {e}"
            ) from e
        return result.endswith(" 1")

    async def list_teams_for_user_detailed(self, user_id: str) -> List[Dict[str, Any]]:
        """Teams the user belongs to, with names + the caller's role in each.
        Powers the team-management UI (get_teams_for_user returns bare ids for
        the resolver hot path)."""
        await self._ensure_pool_fresh()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT t.id, t.name, t.created_by, tm.role
                FROM team_members tm JOIN teams t ON t.id = tm.team_id
                WHERE tm.user_id = $1 ORDER BY t.name
                """,
                user_id,
            )
            return [
                {"id": r["id"], "name": r["name"],
                 "createdBy": r["created_by"], "role": r["role"]}
                for r in rows
            ]

    async def list_team_members(self, team_id: str) -> List[Dict[str, Any]]:
        """Members of a team, with their email (for display) and role."""
        await self._ensure_pool_fresh()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT tm.user_id, tm.role, u.email
                FROM team_members tm LEFT JOIN users u ON u.id = tm.user_id
                WHERE tm.team_id = $1 ORDER BY tm.role, tm.user_id
                """,
                team_id,
            )
            return [
                {"userId": r["user_id"], "role": r["role"], "email": r["email"]}
                for r in rows
            ]

    async def is_team_member(self, team_id: str, user_id: str) -> bool:
        """True if user_id belongs to team_id. Used to authorize team-scoped
        reads/management (deny-by-default: missing row → False)."""
        await self._ensure_pool_fresh()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM team_members WHERE team_id = $1 AND user_id = $2",
                team_id, user_id,
            )
            return row is not None

    async def get_team_role(self, team_id: str, user_id: str) -> Optional[str]:
        """The caller's role in a team ('admin'/'member'), or None if not a
        member. Used to gate admin-only team operations."""
        await self._ensure_pool_fresh()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT role FROM team_members WHERE team_id = $1 AND user_id = $2",
                team_id, user_id,
            )
            return row["role"] if row else None

    async def close(self):
        """Close all connections in the pool. Sets _closed so any
        further operation raises a clean error instead of trying to
        recreate the pool (process is exiting)."""
        self._closed = True
        if self._pool:
            await self._pool.close()
