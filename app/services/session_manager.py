"""
Session manager for AI tracking.

Manages AI generation sessions per page, including:
- Session creation with unique IDs
- Session retrieval and updates
- Token totals aggregation
"""

import logging
import uuid
from typing import Optional
from datetime import datetime

from app.config import settings
from app.db.connection import get_connection, is_pool_available
from app.db.models import (
    AiSession,
    AiSessionCreate,
    SessionStatus,
)

logger = logging.getLogger(__name__)


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

        Returns:
            Created session or None if tracking disabled
        """
        if not is_pool_available():
            logger.debug("Database not available, session tracking disabled")
            return None

        session_id = generate_session_id(client_code, object_name)

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        INSERT INTO ai_tracking_sessions (
                            SESSION_ID, CLIENT_CODE, CLIENT_ID, USER_ID,
                            OBJECT_NAME, AGENT_NAME, APP_CODE,
                            STATUS, CONTEXT_LIMIT, CREATED_BY
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            session_id,
                            client_code,
                            client_id,
                            user_id,
                            object_name,
                            agent_name,
                            app_code,
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
        """
        Get session by session_id string.

        Args:
            session_id: Session ID string

        Returns:
            Session or None if not found
        """
        if not is_pool_available():
            return None

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        SELECT ID, SESSION_ID, CLIENT_CODE, CLIENT_ID, USER_ID,
                               OBJECT_NAME, AGENT_NAME, APP_CODE, STATUS,
                               TOTAL_INPUT_TOKENS, TOTAL_OUTPUT_TOKENS,
                               TOTAL_CACHE_READ_TOKENS, TOTAL_CACHE_CREATION_TOKENS,
                               REQUEST_COUNT, TURN_COUNT,
                               CONTEXT_TOKENS_USED, CONTEXT_LIMIT,
                               CREATED_BY, CREATED_AT, UPDATED_BY, UPDATED_AT
                        FROM ai_tracking_sessions
                        WHERE SESSION_ID = %s
                        """,
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
        """
        Get session by database ID.

        Args:
            id: Database ID

        Returns:
            Session or None if not found
        """
        if not is_pool_available():
            return None

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        SELECT ID, SESSION_ID, CLIENT_CODE, CLIENT_ID, USER_ID,
                               OBJECT_NAME, AGENT_NAME, APP_CODE, STATUS,
                               TOTAL_INPUT_TOKENS, TOTAL_OUTPUT_TOKENS,
                               TOTAL_CACHE_READ_TOKENS, TOTAL_CACHE_CREATION_TOKENS,
                               REQUEST_COUNT, TURN_COUNT,
                               CONTEXT_TOKENS_USED, CONTEXT_LIMIT,
                               CREATED_BY, CREATED_AT, UPDATED_BY, UPDATED_AT
                        FROM ai_tracking_sessions
                        WHERE ID = %s
                        """,
                        (id,)
                    )
                    row = await cursor.fetchone()

                    if row:
                        return self._row_to_session(row)
                    return None

        except Exception as e:
            logger.error(f"Failed to get session by id {id}: {e}")
            return None

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
        """
        Update session token totals.

        Args:
            session_id: Session ID
            input_tokens: Input tokens to add
            output_tokens: Output tokens to add
            cache_read_tokens: Cache read tokens to add
            cache_creation_tokens: Cache creation tokens to add
            context_tokens: Context tokens used (absolute, not increment)
            user_id: User ID for updated_by

        Returns:
            True if successful
        """
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
        """
        Increment the turn count and return the new turn number.

        Args:
            session_id: Session ID
            user_id: User ID for updated_by

        Returns:
            New turn number
        """
        if not is_pool_available():
            return 0

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

    async def complete_session(self, session_id: str, user_id: Optional[int] = None) -> bool:
        """
        Mark a session as completed.

        Args:
            session_id: Session ID
            user_id: User ID for updated_by

        Returns:
            True if successful
        """
        if not is_pool_available():
            return False

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

        Column order from SELECT:
        0: ID, 1: SESSION_ID, 2: CLIENT_CODE, 3: CLIENT_ID, 4: USER_ID,
        5: OBJECT_NAME, 6: AGENT_NAME, 7: APP_CODE, 8: STATUS,
        9: TOTAL_INPUT_TOKENS, 10: TOTAL_OUTPUT_TOKENS,
        11: TOTAL_CACHE_READ_TOKENS, 12: TOTAL_CACHE_CREATION_TOKENS,
        13: REQUEST_COUNT, 14: TURN_COUNT,
        15: CONTEXT_TOKENS_USED, 16: CONTEXT_LIMIT,
        17: CREATED_BY, 18: CREATED_AT, 19: UPDATED_BY, 20: UPDATED_AT
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
            status=SessionStatus(row[8]) if row[8] else SessionStatus.ACTIVE,
            total_input_tokens=row[9] or 0,
            total_output_tokens=row[10] or 0,
            total_cache_read_tokens=row[11] or 0,
            total_cache_creation_tokens=row[12] or 0,
            request_count=row[13] or 0,
            turn_count=row[14] or 0,
            context_tokens_used=row[15] or 0,
            context_limit=row[16] or settings.CONTEXT_LIMIT_DEFAULT,
            created_by=row[17],
            created_at=row[18],
            updated_by=row[19],
            updated_at=row[20],
        )


# Singleton instance
_session_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    """Get the session manager singleton."""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager
