"""
Context manager for AI conversation history.

Manages multi-turn conversation context:
- Stores conversation turns (user instructions + summaries)
- Builds context strings for LLM calls
- Manages context window limits with smart truncation
"""

import logging
import json
from typing import Optional, List, Dict, Any, Tuple

from app.config import settings
from app.db.connection import get_connection, is_pool_available
from app.db.models import (
    AiSessionHistory,
    AiSessionHistoryCreate,
    ContextUsage,
)
from app.services.session_manager import get_session_manager

logger = logging.getLogger(__name__)

# Approximate tokens per character (for estimation)
TOKENS_PER_CHAR = 0.25


def estimate_tokens(text: str) -> int:
    """Estimate token count for a string (rough approximation)."""
    if not text:
        return 0
    return int(len(text) * TOKENS_PER_CHAR)


class ContextManager:
    """Manages conversation history for multi-turn context."""

    async def add_turn(
        self,
        session_id: str,
        request_id: str,
        turn_number: int,
        user_instruction: str,
        assistant_summary: Optional[str] = None,
        page_snapshot: Optional[str] = None,
        tool_calls_json: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Optional[AiSessionHistory]:
        """
        Add a conversation turn to the history.

        Args:
            session_id: Session ID
            request_id: Request ID for this turn
            turn_number: Sequential turn number
            user_instruction: User's prompt/instruction
            assistant_summary: Summary of what was generated/changed
            page_snapshot: JSON snapshot of page after this turn
            tool_calls_json: JSON array of tool calls made during this turn,
                each entry: {tool, input, success, summary}
            model: LLM model name used for this turn

        Returns:
            Created history entry or None if failed
        """
        if not is_pool_available():
            self._save_turn_file(session_id, turn_number, user_instruction,
                                assistant_summary, tool_calls_json, model)
            return None

        # Estimate tokens used for this turn's context
        input_tokens_used = (
            estimate_tokens(user_instruction) +
            estimate_tokens(assistant_summary or "") +
            estimate_tokens(page_snapshot or "")
        )

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        INSERT INTO ai_session_history (
                            SESSION_ID, REQUEST_ID, TURN_NUMBER,
                            USER_INSTRUCTION, ASSISTANT_SUMMARY, TOOL_CALLS_JSON,
                            MODEL, PAGE_SNAPSHOT, INPUT_TOKENS_USED
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            session_id,
                            request_id,
                            turn_number,
                            user_instruction,
                            assistant_summary,
                            tool_calls_json,
                            model,
                            page_snapshot,
                            input_tokens_used,
                        )
                    )

                    record_id = cursor.lastrowid

            logger.debug(f"Added turn {turn_number} to session {session_id}")

            return AiSessionHistory(
                id=record_id,
                session_id=session_id,
                request_id=request_id,
                turn_number=turn_number,
                user_instruction=user_instruction,
                assistant_summary=assistant_summary,
                model=model,
                page_snapshot=page_snapshot,
                input_tokens_used=input_tokens_used,
            )

        except Exception as e:
            logger.error(f"Failed to add turn: {e}")
            return None

    async def upsert_turn(
        self,
        session_id: str,
        request_id: str,
        turn_number: int,
        user_instruction: str,
        assistant_summary: Optional[str] = None,
        tool_calls_json: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        """Insert or update a turn for incremental persistence.

        Uses INSERT ... ON DUPLICATE KEY UPDATE so that partial progress
        during a tool-use loop can be saved after each cycle.
        Requires a UNIQUE index on (SESSION_ID, TURN_NUMBER).
        """
        if not is_pool_available():
            self._save_turn_file(session_id, turn_number, user_instruction,
                                assistant_summary, tool_calls_json, model)
            return

        input_tokens_used = (
            estimate_tokens(user_instruction) +
            estimate_tokens(assistant_summary or "")
        )

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        INSERT INTO ai_session_history (
                            SESSION_ID, REQUEST_ID, TURN_NUMBER,
                            USER_INSTRUCTION, ASSISTANT_SUMMARY, TOOL_CALLS_JSON,
                            MODEL, INPUT_TOKENS_USED
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            ASSISTANT_SUMMARY = VALUES(ASSISTANT_SUMMARY),
                            TOOL_CALLS_JSON = VALUES(TOOL_CALLS_JSON),
                            MODEL = VALUES(MODEL),
                            INPUT_TOKENS_USED = VALUES(INPUT_TOKENS_USED)
                        """,
                        (
                            session_id,
                            request_id,
                            turn_number,
                            user_instruction,
                            assistant_summary,
                            tool_calls_json,
                            model,
                            input_tokens_used,
                        )
                    )
        except Exception as e:
            logger.error(f"Failed to upsert turn: {e}")

    async def get_history(
        self,
        session_id: str,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> Tuple[List[AiSessionHistory], int]:
        """
        Get conversation history for a session.

        Args:
            session_id: Session ID
            limit: Maximum number of turns to return (most recent)
            offset: Number of most-recent turns to skip (for pagination)

        Returns:
            Tuple of (history entries ordered by turn number, total count)
        """
        if not is_pool_available():
            return self._get_history_file(session_id, limit, offset)

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    # Get total count
                    await cursor.execute(
                        "SELECT COUNT(*) FROM ai_session_history WHERE SESSION_ID = %s",
                        (session_id,)
                    )
                    count_row = await cursor.fetchone()
                    total = count_row[0] if count_row else 0

                    if limit:
                        # Paginate from newest: skip `offset` newest, take `limit`
                        # Subquery gets the right slice in DESC order,
                        # outer query re-orders ASC
                        await cursor.execute(
                            """
                            SELECT * FROM (
                                SELECT ID, SESSION_ID, REQUEST_ID, TURN_NUMBER,
                                       USER_INSTRUCTION, ASSISTANT_SUMMARY,
                                       TOOL_CALLS_JSON, MODEL, PAGE_SNAPSHOT,
                                       INPUT_TOKENS_USED, CREATED_AT
                                FROM ai_session_history
                                WHERE SESSION_ID = %s
                                ORDER BY TURN_NUMBER DESC
                                LIMIT %s OFFSET %s
                            ) AS sub
                            ORDER BY TURN_NUMBER ASC
                            """,
                            (session_id, limit, offset)
                        )
                    else:
                        await cursor.execute(
                            """
                            SELECT ID, SESSION_ID, REQUEST_ID, TURN_NUMBER,
                                   USER_INSTRUCTION, ASSISTANT_SUMMARY,
                                   TOOL_CALLS_JSON, MODEL, PAGE_SNAPSHOT,
                                   INPUT_TOKENS_USED, CREATED_AT
                            FROM ai_session_history
                            WHERE SESSION_ID = %s
                            ORDER BY TURN_NUMBER
                            """,
                            (session_id,)
                        )

                    rows = await cursor.fetchall()
                    return [self._row_to_history(row) for row in rows], total

        except Exception as e:
            logger.error(f"Failed to get history for session {session_id}: {e}")
            return [], 0

    async def build_context_string(
        self,
        session_id: str,
        max_tokens: int = 50000,
        include_page_snapshot: bool = True,
    ) -> str:
        """
        Build a context string from conversation history.

        Creates a formatted string suitable for including in LLM prompts.
        Respects max_tokens limit by truncating older turns.

        Args:
            session_id: Session ID
            max_tokens: Maximum tokens for context (default 50K)
            include_page_snapshot: Whether to include page JSON snapshots

        Returns:
            Formatted context string
        """
        history, _ = await self.get_history(session_id)

        if not history:
            return ""

        # Build context from most recent to oldest, stopping when we hit limit
        context_parts = []
        total_tokens = 0

        # Process in reverse (most recent first) to prioritize recent context
        for turn in reversed(history):
            turn_text = self._format_turn(turn, include_page_snapshot)
            turn_tokens = estimate_tokens(turn_text)

            if total_tokens + turn_tokens > max_tokens:
                # We've hit the limit - add a note about truncated history
                context_parts.insert(0, "[Earlier conversation history truncated...]")
                break

            context_parts.insert(0, turn_text)
            total_tokens += turn_tokens

        if not context_parts:
            return ""

        return "## Previous Conversation Context\n\n" + "\n\n".join(context_parts)

    def _format_turn(self, turn: AiSessionHistory, include_snapshot: bool = True) -> str:
        """Format a single turn for context."""
        parts = [f"**Turn {turn.turn_number}:** User asked: \"{turn.user_instruction}\""]

        if turn.assistant_summary:
            parts.append(f"Result: {turn.assistant_summary}")

        if include_snapshot and turn.page_snapshot:
            # For large snapshots, just include a summary
            try:
                page_data = json.loads(turn.page_snapshot)
                # Extract key info from page
                component_count = len(page_data.get("componentMap", {}))
                parts.append(f"[Page state: {component_count} components]")
            except (json.JSONDecodeError, TypeError):
                pass

        return "\n".join(parts)

    async def get_context_usage(self, session_id: str) -> Optional[ContextUsage]:
        """
        Get context usage information for a session.

        Args:
            session_id: Session ID

        Returns:
            ContextUsage object or None if session not found
        """
        session_manager = get_session_manager()
        session = await session_manager.get_session(session_id)

        if not session:
            return None

        return ContextUsage.from_session(session)

    async def update_context_usage(
        self,
        session_id: str,
        context_tokens: int,
    ) -> bool:
        """
        Update the context tokens used in a session.

        Args:
            session_id: Session ID
            context_tokens: New context token count

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
                        SET CONTEXT_TOKENS_USED = %s
                        WHERE SESSION_ID = %s
                        """,
                        (context_tokens, session_id)
                    )
                    return cursor.rowcount > 0

        except Exception as e:
            logger.error(f"Failed to update context usage: {e}")
            return False

    async def truncate_old_history(
        self,
        session_id: str,
        keep_turns: int = 5,
    ) -> int:
        """
        Truncate old conversation history, keeping only recent turns.

        This is useful when context is getting too large.

        Args:
            session_id: Session ID
            keep_turns: Number of recent turns to keep

        Returns:
            Number of turns deleted
        """
        if not is_pool_available():
            return 0

        try:
            # Get max turn number
            history, _ = await self.get_history(session_id)
            if len(history) <= keep_turns:
                return 0  # Nothing to truncate

            max_turn = max(h.turn_number for h in history)
            cutoff_turn = max_turn - keep_turns

            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        DELETE FROM ai_session_history
                        WHERE SESSION_ID = %s AND TURN_NUMBER <= %s
                        """,
                        (session_id, cutoff_turn)
                    )

                    deleted = cursor.rowcount
                    logger.info(f"Truncated {deleted} old turns from session {session_id}")
                    return deleted

        except Exception as e:
            logger.error(f"Failed to truncate history: {e}")
            return 0

    def _row_to_history(self, row: tuple) -> AiSessionHistory:
        """Convert database row to AiSessionHistory model.

        Column order from SELECT:
        0: ID, 1: SESSION_ID, 2: REQUEST_ID, 3: TURN_NUMBER,
        4: USER_INSTRUCTION, 5: ASSISTANT_SUMMARY,
        6: TOOL_CALLS_JSON, 7: MODEL, 8: PAGE_SNAPSHOT,
        9: INPUT_TOKENS_USED, 10: CREATED_AT
        """
        return AiSessionHistory(
            id=row[0],
            session_id=row[1],
            request_id=row[2],
            turn_number=row[3],
            user_instruction=row[4],
            assistant_summary=row[5],
            tool_calls_json=row[6],
            model=row[7],
            page_snapshot=row[8],
            input_tokens_used=row[9] or 0,
            created_at=row[10],
        )


    # ── File-backed helpers (standalone mode without MySQL) ────────

    def _save_turn_file(
        self, session_id, turn_number, user_instruction,
        assistant_summary, tool_calls_json, model,
    ) -> None:
        try:
            from app.db.file_store import get_file_store
            get_file_store().save_turn(
                session_id, turn_number,
                user_instruction or "",
                assistant_summary or "",
                tool_calls_json, model,
            )
        except Exception as e:
            logger.warning(f"Failed to save turn to file store: {e}")

    def _get_history_file(
        self, session_id, limit, offset,
    ) -> Tuple[List[AiSessionHistory], int]:
        try:
            from app.db.file_store import get_file_store
            turns, total = get_file_store().load_history(
                session_id, limit=limit or 100, offset=offset,
            )
            history = []
            for t in turns:
                history.append(AiSessionHistory(
                    id=0,
                    session_id=session_id,
                    request_id="",
                    turn_number=t.get("turn_number", 0),
                    user_instruction=t.get("user_instruction"),
                    assistant_summary=t.get("assistant_summary"),
                    tool_calls_json=t.get("tool_calls_json"),
                    model=t.get("model"),
                ))
            return history, total
        except Exception as e:
            logger.warning(f"Failed to load history from file store: {e}")
            return [], 0


# Singleton instance
_context_manager: Optional[ContextManager] = None


def get_context_manager() -> ContextManager:
    """Get the context manager singleton."""
    global _context_manager
    if _context_manager is None:
        _context_manager = ContextManager()
    return _context_manager
