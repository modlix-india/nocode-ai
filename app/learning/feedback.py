"""Feedback collection - explicit and implicit signal tracking.

Explicit: User clicks thumbs up/down, provides text corrections.
Implicit: Detected from session patterns - retries, undos, abandonments.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.db.connection import get_connection, is_pool_available
from app.learning.models import FeedbackCreate, FeedbackType

logger = logging.getLogger(__name__)


class FeedbackCollector:
    """Collects and stores user feedback signals."""

    async def record_explicit_feedback(
        self,
        feedback: FeedbackCreate,
        client_code: str,
        user_id: int,
        agent_name: str,
    ) -> Optional[int]:
        """Store explicit user feedback (thumbs up/down + optional text).

        Denormalizes user_instruction, assistant_summary, and
        tool_calls_json from ai_session_history so feedback records
        are self-contained for analysis even if the session is deleted.

        Returns the inserted feedback ID or None on failure.
        """
        if not is_pool_available():
            return None

        # Fetch turn data for denormalization
        user_instruction = None
        assistant_summary = None
        tool_calls_json = None
        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """SELECT USER_INSTRUCTION, ASSISTANT_SUMMARY, TOOL_CALLS_JSON
                           FROM ai_session_history
                           WHERE SESSION_ID = %s AND TURN_NUMBER = %s""",
                        (feedback.session_id, feedback.turn_number),
                    )
                    row = await cursor.fetchone()
                    if row:
                        user_instruction = row[0]
                        assistant_summary = row[1]
                        tool_calls_json = row[2]
        except Exception as e:
            logger.warning("Failed to fetch turn data for feedback denormalization: %s", e)

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """INSERT INTO ai_learning_feedback
                           (SESSION_ID, TURN_NUMBER, CLIENT_CODE, USER_ID,
                            AGENT_NAME, RATING, FEEDBACK_TEXT, FEEDBACK_TYPE,
                            USER_INSTRUCTION, ASSISTANT_SUMMARY, TOOL_CALLS_JSON)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (
                            feedback.session_id, feedback.turn_number,
                            client_code, user_id, agent_name,
                            feedback.rating, feedback.feedback_text,
                            feedback.feedback_type.value,
                            user_instruction, assistant_summary, tool_calls_json,
                        ),
                    )
                    return cursor.lastrowid
        except Exception as e:
            logger.error("Failed to record feedback: %s", e)
            return None

    async def detect_implicit_signals(self, session_id: str) -> dict:
        """Analyze a completed session for implicit feedback signals.

        Detects:
        - RETRY: Same or very similar user instruction repeated consecutively
        - UNDO: User asks agent to undo/revert previous action
        - ABANDONMENT: Session left in PROCESSING state for >10 minutes

        Returns dict: {"retry_count": int, "undo_count": int, "abandoned": bool}
        """
        result = {"retry_count": 0, "undo_count": 0, "abandoned": False}

        if not is_pool_available():
            return result

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    # Check for abandonment
                    await cursor.execute(
                        """SELECT STATUS, TIMESTAMPDIFF(MINUTE, UPDATED_AT, NOW())
                           FROM ai_tracking_sessions WHERE SESSION_ID = %s""",
                        (session_id,),
                    )
                    row = await cursor.fetchone()
                    if row and row[0] == "PROCESSING" and (row[1] or 0) > 10:
                        result["abandoned"] = True

                    # Check for retries and undos in history
                    await cursor.execute(
                        """SELECT USER_INSTRUCTION FROM ai_session_history
                           WHERE SESSION_ID = %s ORDER BY TURN_NUMBER""",
                        (session_id,),
                    )
                    instructions = [r[0] for r in await cursor.fetchall()]

                    undo_keywords = [
                        "undo", "revert", "go back", "remove that",
                        "delete that", "that's wrong", "not what i asked",
                    ]

                    for i in range(1, len(instructions)):
                        curr = (instructions[i] or "").lower().strip()
                        prev = (instructions[i - 1] or "").lower().strip()

                        if curr and prev and _jaccard_similarity(curr, prev) > 0.8:
                            result["retry_count"] += 1

                        if any(kw in curr for kw in undo_keywords):
                            result["undo_count"] += 1

        except Exception as e:
            logger.error("Failed to detect implicit signals for %s: %s", session_id, e)

        return result


def _jaccard_similarity(a: str, b: str) -> float:
    """Quick Jaccard word-overlap similarity (no external deps)."""
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


# Singleton
_feedback_collector: Optional[FeedbackCollector] = None


def get_feedback_collector() -> FeedbackCollector:
    global _feedback_collector
    if _feedback_collector is None:
        _feedback_collector = FeedbackCollector()
    return _feedback_collector
