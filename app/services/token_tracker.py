"""
Token tracker for AI usage tracking.

Records individual LLM call token usage to the database.
Updates session totals after recording.
"""

import logging
from typing import Optional, List

from app.db.connection import get_connection, is_pool_available
from app.db.models import (
    AiTokenUsage,
    AiTokenUsageCreate,
)
from app.services.session_manager import get_session_manager

logger = logging.getLogger(__name__)


class TokenTracker:
    """Tracks and persists token usage."""

    async def record_usage(self, usage: AiTokenUsageCreate) -> Optional[AiTokenUsage]:
        """
        Record a single token usage entry.

        Args:
            usage: Token usage data

        Returns:
            Created record or None if failed
        """
        if not is_pool_available():
            logger.debug("Database not available, token tracking disabled")
            return None

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        INSERT INTO ai_tracking_token_usage (
                            SESSION_ID, REQUEST_ID, CLIENT_CODE, CLIENT_ID, USER_ID,
                            AGENT_TYPE, MODEL, LLM_PROVIDER,
                            INPUT_TOKENS, OUTPUT_TOKENS,
                            CACHE_READ_TOKENS, CACHE_CREATION_TOKENS,
                            LATENCY_MS, SUCCESS, ERROR_MESSAGE,
                            CREATED_BY
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            usage.session_id,
                            usage.request_id,
                            usage.client_code,
                            usage.client_id,
                            usage.user_id,
                            usage.agent_type,
                            usage.model,
                            usage.llm_provider,
                            usage.input_tokens,
                            usage.output_tokens,
                            usage.cache_read_tokens,
                            usage.cache_creation_tokens,
                            usage.latency_ms,
                            1 if usage.success else 0,
                            usage.error_message,
                            usage.user_id,
                        )
                    )

                    record_id = cursor.lastrowid

            logger.debug(f"Recorded token usage for {usage.agent_type}: {usage.input_tokens}+{usage.output_tokens} tokens")

            # Return a model with the ID
            return AiTokenUsage(
                id=record_id,
                **usage.model_dump()
            )

        except Exception as e:
            logger.error(f"Failed to record token usage: {e}")
            return None

    async def record_usage_batch(
        self,
        usages: List[AiTokenUsageCreate],
        update_session: bool = True,
    ) -> List[AiTokenUsage]:
        """
        Record multiple token usage entries in batch.

        Args:
            usages: List of token usage data
            update_session: Whether to update session totals

        Returns:
            List of created records (may be partial if some failed)
        """
        if not usages:
            return []

        if not is_pool_available():
            logger.debug("Database not available, token tracking disabled")
            return []

        results = []

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    # Insert all records
                    for usage in usages:
                        await cursor.execute(
                            """
                            INSERT INTO ai_tracking_token_usage (
                                SESSION_ID, REQUEST_ID, CLIENT_CODE, CLIENT_ID, USER_ID,
                                AGENT_TYPE, MODEL, LLM_PROVIDER,
                                INPUT_TOKENS, OUTPUT_TOKENS,
                                CACHE_READ_TOKENS, CACHE_CREATION_TOKENS,
                                LATENCY_MS, SUCCESS, ERROR_MESSAGE,
                                CREATED_BY
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                usage.session_id,
                                usage.request_id,
                                usage.client_code,
                                usage.client_id,
                                usage.user_id,
                                usage.agent_type,
                                usage.model,
                                usage.llm_provider,
                                usage.input_tokens,
                                usage.output_tokens,
                                usage.cache_read_tokens,
                                usage.cache_creation_tokens,
                                usage.latency_ms,
                                1 if usage.success else 0,
                                usage.error_message,
                                usage.user_id,
                            )
                        )

                        results.append(AiTokenUsage(
                            id=cursor.lastrowid,
                            **usage.model_dump()
                        ))

            logger.info(f"Recorded {len(results)} token usage entries")

            # Update session totals
            if update_session and usages:
                await self._update_session_totals(usages)

            return results

        except Exception as e:
            logger.error(f"Failed to record token usage batch: {e}")
            return results

    async def _update_session_totals(self, usages: List[AiTokenUsageCreate]) -> None:
        """
        Update session totals based on usage records.

        Args:
            usages: List of token usage data
        """
        if not usages:
            return

        # Aggregate totals
        total_input = sum(u.input_tokens for u in usages)
        total_output = sum(u.output_tokens for u in usages)
        total_cache_read = sum(u.cache_read_tokens for u in usages)
        total_cache_creation = sum(u.cache_creation_tokens for u in usages)

        # Get session ID and user ID from first usage
        session_id = usages[0].session_id
        user_id = usages[0].user_id

        # Calculate context tokens (input + cache read is what's used for context)
        # This is a rough estimate; actual context depends on conversation history
        context_tokens = total_input + total_cache_read

        session_manager = get_session_manager()
        await session_manager.update_session_totals(
            session_id=session_id,
            input_tokens=total_input,
            output_tokens=total_output,
            cache_read_tokens=total_cache_read,
            cache_creation_tokens=total_cache_creation,
            context_tokens=context_tokens,
            user_id=user_id,
        )

    async def get_usage_by_session(self, session_id: str) -> List[AiTokenUsage]:
        """
        Get all token usage records for a session.

        Args:
            session_id: Session ID

        Returns:
            List of token usage records
        """
        if not is_pool_available():
            return []

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        SELECT ID, SESSION_ID, REQUEST_ID, CLIENT_CODE, CLIENT_ID, USER_ID,
                               AGENT_TYPE, MODEL, LLM_PROVIDER,
                               INPUT_TOKENS, OUTPUT_TOKENS,
                               CACHE_READ_TOKENS, CACHE_CREATION_TOKENS,
                               LATENCY_MS, SUCCESS, ERROR_MESSAGE,
                               CREATED_BY, CREATED_AT, UPDATED_BY, UPDATED_AT
                        FROM ai_tracking_token_usage
                        WHERE SESSION_ID = %s
                        ORDER BY CREATED_AT
                        """,
                        (session_id,)
                    )
                    rows = await cursor.fetchall()

                    return [self._row_to_usage(row) for row in rows]

        except Exception as e:
            logger.error(f"Failed to get usage for session {session_id}: {e}")
            return []

    async def get_usage_by_request(self, request_id: str) -> List[AiTokenUsage]:
        """
        Get all token usage records for a request.

        Args:
            request_id: Request ID

        Returns:
            List of token usage records
        """
        if not is_pool_available():
            return []

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        SELECT ID, SESSION_ID, REQUEST_ID, CLIENT_CODE, CLIENT_ID, USER_ID,
                               AGENT_TYPE, MODEL, LLM_PROVIDER,
                               INPUT_TOKENS, OUTPUT_TOKENS,
                               CACHE_READ_TOKENS, CACHE_CREATION_TOKENS,
                               LATENCY_MS, SUCCESS, ERROR_MESSAGE,
                               CREATED_BY, CREATED_AT, UPDATED_BY, UPDATED_AT
                        FROM ai_tracking_token_usage
                        WHERE REQUEST_ID = %s
                        ORDER BY CREATED_AT
                        """,
                        (request_id,)
                    )
                    rows = await cursor.fetchall()

                    return [self._row_to_usage(row) for row in rows]

        except Exception as e:
            logger.error(f"Failed to get usage for request {request_id}: {e}")
            return []

    def _row_to_usage(self, row: tuple) -> AiTokenUsage:
        """Convert database row to AiTokenUsage model."""
        return AiTokenUsage(
            id=row[0],
            session_id=row[1],
            request_id=row[2],
            client_code=row[3],
            client_id=row[4],
            user_id=row[5],
            agent_type=row[6],
            model=row[7],
            llm_provider=row[8],
            input_tokens=row[9] or 0,
            output_tokens=row[10] or 0,
            cache_read_tokens=row[11] or 0,
            cache_creation_tokens=row[12] or 0,
            latency_ms=row[13],
            success=bool(row[14]),
            error_message=row[15],
            created_by=row[16],
            created_at=row[17],
            updated_by=row[18],
            updated_at=row[19],
        )


# Singleton instance
_token_tracker: Optional[TokenTracker] = None


def get_token_tracker() -> TokenTracker:
    """Get the token tracker singleton."""
    global _token_tracker
    if _token_tracker is None:
        _token_tracker = TokenTracker()
    return _token_tracker
