"""
Session manager for AI tracking.

Manages AI generation sessions per page, including:
- Session creation with unique IDs
- Session retrieval, listing, and updates
- Token totals aggregation
"""

import logging
import uuid
from typing import Any, Optional, List, Tuple

from app.config import settings
from app.db.connection import get_connection, is_pool_available
from app.db.models import (
    AiSession,
    AiSessionCreate,
    SessionStatus,
)

logger = logging.getLogger(__name__)

# Common SELECT columns for session queries (order matters — matches _row_to_session)
_SESSION_COLUMNS = """
    ID, SESSION_ID, CLIENT_CODE, CLIENT_ID, USER_ID,
    OBJECT_NAME, AGENT_NAME, APP_CODE, TITLE, CONTEXT_JSON, STATUS,
    TOTAL_INPUT_TOKENS, TOTAL_OUTPUT_TOKENS,
    TOTAL_CACHE_READ_TOKENS, TOTAL_CACHE_CREATION_TOKENS,
    REQUEST_COUNT, TURN_COUNT,
    CONTEXT_TOKENS_USED, CONTEXT_LIMIT,
    CREATED_BY, CREATED_AT, UPDATED_BY, UPDATED_AT
"""


def generate_short_uuid() -> str:
    """Generate a short UUID (8 characters)."""
    return uuid.uuid4().hex[:8]


def generate_session_id(client_code: str, object_name: Optional[str] = None) -> str:
    """
    Generate session ID in format: clientCode_objectName_shortUUID

    Examples:
        - "ACME_loginPage_a1b2c3d4"
        - "ACME_a1b2c3d4" (if no object name)
    """
    short_uuid = generate_short_uuid()
    if object_name:
        # Sanitize object name (keep alphanumeric and underscores, limit length)
        safe_name = "".join(c for c in object_name if c.isalnum() or c == "_")[:32]
        return f"{client_code}_{safe_name}_{short_uuid}"
    return f"{client_code}_{short_uuid}"


class SessionManager:
    """Manages AI generation sessions."""

    async def create_session(
        self,
        client_code: str,
        client_id: int,
        user_id: int,
        object_name: Optional[str] = None,
        agent_name: Optional[str] = None,
        app_code: Optional[str] = None,
        title: Optional[str] = None,
        context_json: Optional[str] = None,
    ) -> Optional[AiSession]:
        """
        Create a new session.

        Args:
            client_code: Client code
            client_id: Client ID
            user_id: User ID
            object_name: Optional object name (page name, function name, etc.)
            agent_name: Optional agent name (PageAgent, FunctionAgent, etc.)
            app_code: Optional app code (sitezump/appbuilder)
            title: Optional session title for sidebar display
            context_json: Optional JSON-serialized agent context

        Returns:
            Created session or None if tracking disabled
        """
        if not is_pool_available():
            return self._create_session_file(
                client_code, client_id, user_id, object_name,
                agent_name, app_code, title, context_json,
            )

        session_id = generate_session_id(client_code, object_name)

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        INSERT INTO ai_tracking_sessions (
                            SESSION_ID, CLIENT_CODE, CLIENT_ID, USER_ID,
                            OBJECT_NAME, AGENT_NAME, APP_CODE, TITLE,
                            CONTEXT_JSON, STATUS, CONTEXT_LIMIT, CREATED_BY
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            session_id,
                            client_code,
                            client_id,
                            user_id,
                            object_name,
                            agent_name,
                            app_code,
                            title,
                            context_json,
                            SessionStatus.ACTIVE.value,
                            settings.CONTEXT_LIMIT_DEFAULT,
                            user_id,
                        )
                    )

            logger.info(f"Created session: {session_id} (agent: {agent_name}, object: {object_name})")
            return await self.get_session(session_id)

        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            return None

    async def get_session(self, session_id: str) -> Optional[AiSession]:
        """Get session by session_id string."""
        if not is_pool_available():
            return self._get_session_file(session_id)

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        f"SELECT {_SESSION_COLUMNS} FROM ai_tracking_sessions WHERE SESSION_ID = %s",
                        (session_id,)
                    )
                    row = await cursor.fetchone()
                    if row:
                        return self._row_to_session(row)
                    return None

        except Exception as e:
            logger.error(f"Failed to get session {session_id}: {e}")
            return None

    async def get_session_by_id(self, id: int) -> Optional[AiSession]:
        """Get session by database ID."""
        if not is_pool_available():
            return None

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        f"SELECT {_SESSION_COLUMNS} FROM ai_tracking_sessions WHERE ID = %s",
                        (id,)
                    )
                    row = await cursor.fetchone()
                    if row:
                        return self._row_to_session(row)
                    return None

        except Exception as e:
            logger.error(f"Failed to get session by id {id}: {e}")
            return None

    async def list_sessions(
        self,
        user_id: int,
        client_code: str,
        agent_name: Optional[str] = None,
        status: Optional[SessionStatus] = None,
        app_code: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Tuple[List[AiSession], int]:
        """List sessions for a user, ordered by updated_at DESC.

        Args:
            user_id: Filter by user ID
            client_code: Filter by client code
            agent_name: Optional filter by agent name
            status: Optional filter by session status
            app_code: Optional filter by the app the chat was started against
            limit: Max results (default 20)
            offset: Skip first N results (default 0)

        Returns:
            Tuple of (sessions list, total count)
        """
        if not is_pool_available():
            return self._list_sessions_file(
                user_id, client_code, agent_name, limit, offset, app_code
            )

        try:
            # Build WHERE clause dynamically
            conditions = ["USER_ID = %s", "CLIENT_CODE = %s"]
            params: list = [user_id, client_code]

            if agent_name:
                conditions.append("AGENT_NAME = %s")
                params.append(agent_name)
            if status:
                conditions.append("STATUS = %s")
                params.append(status.value)
            if app_code:
                conditions.append("APP_CODE = %s")
                params.append(app_code)

            where_clause = " AND ".join(conditions)

            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    # Get total count
                    await cursor.execute(
                        f"SELECT COUNT(*) FROM ai_tracking_sessions WHERE {where_clause}",
                        tuple(params)
                    )
                    count_row = await cursor.fetchone()
                    total = count_row[0] if count_row else 0

                    # Get paginated results
                    await cursor.execute(
                        f"SELECT {_SESSION_COLUMNS} FROM ai_tracking_sessions "
                        f"WHERE {where_clause} ORDER BY UPDATED_AT DESC LIMIT %s OFFSET %s",
                        tuple(params + [limit, offset])
                    )
                    rows = await cursor.fetchall()
                    sessions = [self._row_to_session(row) for row in rows]

                    return sessions, total

        except Exception as e:
            logger.error(f"Failed to list sessions: {e}")
            return [], 0

    async def update_session_title(
        self, session_id: str, title: str, user_id: Optional[int] = None
    ) -> bool:
        """Update session title.

        Args:
            session_id: Session ID
            title: New title (max 256 chars)
            user_id: User ID for updated_by

        Returns:
            True if successful
        """
        if not is_pool_available():
            return self._update_session_field_file(session_id, "title", title[:256])

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        UPDATE ai_tracking_sessions
                        SET TITLE = %s, UPDATED_BY = %s, UPDATED_AT = UPDATED_AT
                        WHERE SESSION_ID = %s
                        """,
                        (title[:256], user_id, session_id)
                    )
                    return cursor.rowcount > 0

        except Exception as e:
            logger.error(f"Failed to update session title: {e}")
            return False

    async def delete_session(self, session_id: str, user_id: int) -> bool:
        """Delete session and all related records.

        Verifies ownership before deleting. Removes related
        ai_session_history and ai_tracking_token_usage rows first.

        Args:
            session_id: Session ID
            user_id: User ID (must match session owner)

        Returns:
            True if deleted, False otherwise
        """
        if not is_pool_available():
            from app.db.file_store import get_file_store
            return get_file_store().delete_session(session_id)

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    # Verify ownership
                    await cursor.execute(
                        "SELECT USER_ID FROM ai_tracking_sessions WHERE SESSION_ID = %s",
                        (session_id,)
                    )
                    row = await cursor.fetchone()
                    if not row:
                        return False
                    if row[0] != user_id:
                        logger.warning(f"User {user_id} tried to delete session owned by {row[0]}")
                        return False

                    # Delete related records first (FK constraints)
                    await cursor.execute(
                        "DELETE FROM ai_session_history WHERE SESSION_ID = %s",
                        (session_id,)
                    )
                    await cursor.execute(
                        "DELETE FROM ai_tracking_token_usage WHERE SESSION_ID = %s",
                        (session_id,)
                    )
                    await cursor.execute(
                        "DELETE FROM ai_tracking_sessions WHERE SESSION_ID = %s",
                        (session_id,)
                    )
                    await conn.commit()

                    logger.info(f"Deleted session: {session_id}")
                    return True

        except Exception as e:
            logger.error(f"Failed to delete session {session_id}: {e}")
            return False

    async def update_session_context(
        self, session_id: str, context_json: str, user_id: Optional[int] = None
    ) -> bool:
        """Update session context JSON.

        Args:
            session_id: Session ID
            context_json: JSON-serialized context string
            user_id: User ID for updated_by

        Returns:
            True if successful
        """
        if not is_pool_available():
            return self._update_session_field_file(session_id, "context_json", context_json)

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        UPDATE ai_tracking_sessions
                        SET CONTEXT_JSON = %s, UPDATED_BY = %s
                        WHERE SESSION_ID = %s
                        """,
                        (context_json, user_id, session_id)
                    )
                    return cursor.rowcount > 0

        except Exception as e:
            logger.error(f"Failed to update session context: {e}")
            return False

    async def update_session_totals(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        context_tokens: int = 0,
        user_id: Optional[int] = None,
    ) -> bool:
        """Update session token totals."""
        if not is_pool_available():
            return False

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        UPDATE ai_tracking_sessions
                        SET TOTAL_INPUT_TOKENS = TOTAL_INPUT_TOKENS + %s,
                            TOTAL_OUTPUT_TOKENS = TOTAL_OUTPUT_TOKENS + %s,
                            TOTAL_CACHE_READ_TOKENS = TOTAL_CACHE_READ_TOKENS + %s,
                            TOTAL_CACHE_CREATION_TOKENS = TOTAL_CACHE_CREATION_TOKENS + %s,
                            CONTEXT_TOKENS_USED = %s,
                            REQUEST_COUNT = REQUEST_COUNT + 1,
                            UPDATED_BY = %s
                        WHERE SESSION_ID = %s
                        """,
                        (
                            input_tokens,
                            output_tokens,
                            cache_read_tokens,
                            cache_creation_tokens,
                            context_tokens,
                            user_id,
                            session_id,
                        )
                    )
                    return cursor.rowcount > 0

        except Exception as e:
            logger.error(f"Failed to update session totals: {e}")
            return False

    async def increment_turn_count(self, session_id: str, user_id: Optional[int] = None) -> int:
        """Increment the turn count and return the new turn number."""
        if not is_pool_available():
            return self._increment_turn_count_file(session_id)

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        UPDATE ai_tracking_sessions
                        SET TURN_COUNT = TURN_COUNT + 1,
                            UPDATED_BY = %s
                        WHERE SESSION_ID = %s
                        """,
                        (user_id, session_id)
                    )

                    # Get the new turn count
                    await cursor.execute(
                        "SELECT TURN_COUNT FROM ai_tracking_sessions WHERE SESSION_ID = %s",
                        (session_id,)
                    )
                    row = await cursor.fetchone()
                    return row[0] if row else 0

        except Exception as e:
            logger.error(f"Failed to increment turn count: {e}")
            return 0

    async def set_session_processing(self, session_id: str, user_id: Optional[int] = None) -> bool:
        """Mark a session as currently being processed by the agent."""
        if not is_pool_available():
            return self._update_session_field_file(session_id, "status", SessionStatus.PROCESSING.value)

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        UPDATE ai_tracking_sessions
                        SET STATUS = %s, UPDATED_BY = %s
                        WHERE SESSION_ID = %s
                        """,
                        (SessionStatus.PROCESSING.value, user_id, session_id)
                    )
                    return cursor.rowcount > 0

        except Exception as e:
            logger.error(f"Failed to set session processing: {e}")
            return False

    async def complete_session(self, session_id: str, user_id: Optional[int] = None) -> bool:
        """Mark a session as completed."""
        if not is_pool_available():
            return self._update_session_field_file(session_id, "status", SessionStatus.COMPLETED.value)

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        UPDATE ai_tracking_sessions
                        SET STATUS = %s, UPDATED_BY = %s
                        WHERE SESSION_ID = %s
                        """,
                        (SessionStatus.COMPLETED.value, user_id, session_id)
                    )
                    return cursor.rowcount > 0

        except Exception as e:
            logger.error(f"Failed to complete session: {e}")
            return False

    def _row_to_session(self, row: tuple) -> AiSession:
        """Convert database row to AiSession model.

        Column order from SELECT (matches _SESSION_COLUMNS):
        0: ID, 1: SESSION_ID, 2: CLIENT_CODE, 3: CLIENT_ID, 4: USER_ID,
        5: OBJECT_NAME, 6: AGENT_NAME, 7: APP_CODE, 8: TITLE, 9: CONTEXT_JSON,
        10: STATUS,
        11: TOTAL_INPUT_TOKENS, 12: TOTAL_OUTPUT_TOKENS,
        13: TOTAL_CACHE_READ_TOKENS, 14: TOTAL_CACHE_CREATION_TOKENS,
        15: REQUEST_COUNT, 16: TURN_COUNT,
        17: CONTEXT_TOKENS_USED, 18: CONTEXT_LIMIT,
        19: CREATED_BY, 20: CREATED_AT, 21: UPDATED_BY, 22: UPDATED_AT
        """
        return AiSession(
            id=row[0],
            session_id=row[1],
            client_code=row[2],
            client_id=row[3],
            user_id=row[4],
            object_name=row[5],
            agent_name=row[6],
            app_code=row[7],
            title=row[8],
            context_json=row[9],
            status=SessionStatus(row[10]) if row[10] else SessionStatus.ACTIVE,
            total_input_tokens=row[11] or 0,
            total_output_tokens=row[12] or 0,
            total_cache_read_tokens=row[13] or 0,
            total_cache_creation_tokens=row[14] or 0,
            request_count=row[15] or 0,
            turn_count=row[16] or 0,
            context_tokens_used=row[17] or 0,
            context_limit=row[18] or settings.CONTEXT_LIMIT_DEFAULT,
            created_by=row[19],
            created_at=row[20],
            updated_by=row[21],
            updated_at=row[22],
        )


    # ── File-backed helpers (standalone mode without MySQL) ────────

    def _create_session_file(
        self, client_code, client_id, user_id, object_name,
        agent_name, app_code, title, context_json,
    ) -> Optional[AiSession]:
        from app.db.file_store import get_file_store
        session_id = generate_session_id(client_code, object_name)
        data = {
            "session_id": session_id,
            "client_code": client_code,
            "client_id": client_id,
            "user_id": user_id,
            "object_name": object_name,
            "agent_name": agent_name,
            "app_code": app_code,
            "title": title,
            "context_json": context_json,
            "status": SessionStatus.ACTIVE.value,
            "turn_count": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
        }
        get_file_store().save_session(session_id, data)
        return self._dict_to_session(data)

    def _get_session_file(self, session_id: str) -> Optional[AiSession]:
        from app.db.file_store import get_file_store
        data = get_file_store().load_session(session_id)
        if not data:
            return None
        return self._dict_to_session(data)

    def _list_sessions_file(
        self, user_id, client_code, agent_name, limit, offset, app_code=None,
    ) -> tuple[list[AiSession], int]:
        from app.db.file_store import get_file_store
        items, total = get_file_store().list_sessions(
            user_id, client_code, agent_name, limit, offset, app_code,
        )
        return [self._dict_to_session(d) for d in items], total

    def _update_session_field_file(self, session_id: str, field: str, value: Any) -> bool:
        from app.db.file_store import get_file_store
        store = get_file_store()
        data = store.load_session(session_id)
        if not data:
            return False
        data[field] = value
        store.save_session(session_id, data)
        return True

    def _increment_turn_count_file(self, session_id: str) -> int:
        from app.db.file_store import get_file_store
        store = get_file_store()
        data = store.load_session(session_id)
        if not data:
            return 0
        data["turn_count"] = data.get("turn_count", 0) + 1
        store.save_session(session_id, data)
        return data["turn_count"]

    def _dict_to_session(self, d: dict) -> AiSession:
        """Convert a file-store dict to an AiSession model."""
        return AiSession(
            id=0,
            session_id=d.get("session_id", ""),
            client_code=d.get("client_code", ""),
            client_id=d.get("client_id", 0),
            user_id=d.get("user_id", 0),
            object_name=d.get("object_name"),
            agent_name=d.get("agent_name"),
            app_code=d.get("app_code"),
            title=d.get("title"),
            context_json=d.get("context_json"),
            status=SessionStatus(d["status"]) if d.get("status") else SessionStatus.ACTIVE,
            total_input_tokens=d.get("total_input_tokens", 0),
            total_output_tokens=d.get("total_output_tokens", 0),
            turn_count=d.get("turn_count", 0),
            context_tokens_used=d.get("context_tokens_used", 0),
            context_limit=d.get("context_limit", settings.CONTEXT_LIMIT_DEFAULT),
        )


# Singleton instance
_session_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    """Get the session manager singleton."""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager
