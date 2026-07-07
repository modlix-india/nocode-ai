"""Outcome analyzer - computes session success scores.

Scoring formula (v1):
  success_score = (
      0.30 * tool_success_rate        # Did tools succeed?
    + 0.25 * user_satisfaction_norm    # User explicit ratings (normalized 0-1)
    + 0.20 * (1 - retry_penalty)      # Did user have to retry?
    + 0.15 * (1 - undo_penalty)       # Did user undo agent work?
    + 0.10 * efficiency_score         # Tokens per successful tool call
  )

Each component is normalized to [0, 1].
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from app.db.connection import get_connection, is_pool_available
from app.learning.models import SessionScore
from app.learning.feedback import get_feedback_collector

logger = logging.getLogger(__name__)

SCORE_VERSION = "v1"


class OutcomeAnalyzer:
    """Computes session-level success scores for the learning loop."""

    async def score_session(self, session_id: str) -> Optional[SessionScore]:
        """Compute and persist the success score for a completed session.

        Called asynchronously after a session completes (via asyncio.create_task).
        Also callable in batch for older un-scored sessions.

        Returns the computed SessionScore or None on failure.
        """
        if not is_pool_available():
            return None

        try:
            session_meta = await self._get_session_meta(session_id)
            if not session_meta:
                return None

            tool_stats = await self._get_tool_stats(session_id)
            feedback_stats = await self._get_feedback_stats(session_id)
            implicit = await get_feedback_collector().detect_implicit_signals(session_id)

            # Sub-scores
            tool_success_rate = (
                tool_stats["success_count"] / max(tool_stats["total_count"], 1)
            )

            user_sat = float(feedback_stats["avg_rating"])  # [-1, 1]
            user_sat_norm = (user_sat + 1) / 2  # normalize to [0, 1]

            retry_penalty = min(
                implicit["retry_count"] / max(session_meta["turn_count"], 1), 1.0
            )
            undo_penalty = min(
                implicit["undo_count"] / max(session_meta["turn_count"], 1), 1.0
            )

            # Efficiency: lower tokens per successful tool call is better
            # 10K tokens/success = 1.0, 100K = 0.0
            # Cast to float - MySQL may return total_tokens as decimal.Decimal,
            # which can't mix with float literals like 1.0 below.
            tokens_per_success = (
                float(session_meta["total_tokens"]) / max(tool_stats["success_count"], 1)
            )
            efficiency_score = max(0.0, 1.0 - (tokens_per_success - 10000) / 90000)
            efficiency_score = min(efficiency_score, 1.0)

            # Weighted composite
            success_score = (
                0.30 * tool_success_rate
                + 0.25 * user_sat_norm
                + 0.20 * (1 - retry_penalty)
                + 0.15 * (1 - undo_penalty)
                + 0.10 * efficiency_score
            )

            score = SessionScore(
                session_id=session_id,
                success_score=round(success_score, 4),
                user_satisfaction=(
                    round(user_sat, 4) if feedback_stats["count"] > 0 else None
                ),
                tool_error_rate=round(1 - tool_success_rate, 4),
                turn_count=session_meta["turn_count"],
                tool_call_count=tool_stats["total_count"],
                retry_count=implicit["retry_count"],
                undo_count=implicit["undo_count"],
                abandoned=implicit["abandoned"],
                total_tokens=session_meta["total_tokens"],
                total_latency_ms=session_meta["total_latency_ms"],
            )

            await self._persist_score(score, session_meta.get("agent_name", ""), session_meta.get("client_code", ""))
            return score

        except Exception as e:
            logger.error("Failed to score session %s: %s", session_id, e)
            return None

    async def _get_tool_stats(self, session_id: str) -> dict:
        """Count successful vs failed tool calls from session history."""
        stats = {"total_count": 0, "success_count": 0, "error_count": 0}
        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """SELECT TOOL_CALLS_JSON FROM ai_session_history
                           WHERE SESSION_ID = %s AND TOOL_CALLS_JSON IS NOT NULL""",
                        (session_id,),
                    )
                    for row in await cursor.fetchall():
                        try:
                            calls = json.loads(row[0])
                            for call in calls:
                                stats["total_count"] += 1
                                if call.get("success"):
                                    stats["success_count"] += 1
                                else:
                                    stats["error_count"] += 1
                        except (json.JSONDecodeError, TypeError):
                            pass
        except Exception as e:
            logger.warning("Failed to get tool stats for %s: %s", session_id, e)
        return stats

    async def _get_feedback_stats(self, session_id: str) -> dict:
        """Get aggregated feedback ratings for a session."""
        stats = {"count": 0, "avg_rating": 0.0}
        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """SELECT COUNT(*), COALESCE(AVG(RATING), 0)
                           FROM ai_learning_feedback
                           WHERE SESSION_ID = %s AND FEEDBACK_TYPE = 'RATING'""",
                        (session_id,),
                    )
                    row = await cursor.fetchone()
                    if row:
                        stats["count"] = row[0] or 0
                        stats["avg_rating"] = float(row[1] or 0)
        except Exception as e:
            logger.warning("Failed to get feedback stats for %s: %s", session_id, e)
        return stats

    async def _get_session_meta(self, session_id: str) -> Optional[dict]:
        """Get session metadata needed for scoring."""
        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """SELECT TURN_COUNT, AGENT_NAME, CLIENT_CODE,
                                  COALESCE(TOTAL_INPUT_TOKENS, 0) + COALESCE(TOTAL_OUTPUT_TOKENS, 0)
                           FROM ai_tracking_sessions WHERE SESSION_ID = %s""",
                        (session_id,),
                    )
                    row = await cursor.fetchone()
                    if row:
                        return {
                            "turn_count": row[0] or 0,
                            "agent_name": row[1] or "",
                            "client_code": row[2] or "",
                            "total_tokens": row[3] or 0,
                            "total_latency_ms": 0,
                        }
        except Exception as e:
            logger.warning("Failed to get session meta for %s: %s", session_id, e)
        return None

    async def _persist_score(self, score: SessionScore, agent_name: str, client_code: str) -> None:
        """Write score to ai_learning_session_scores (upsert)."""
        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """INSERT INTO ai_learning_session_scores
                           (SESSION_ID, AGENT_NAME, CLIENT_CODE, SUCCESS_SCORE,
                            USER_SATISFACTION, TOOL_ERROR_RATE, TURN_COUNT,
                            TOOL_CALL_COUNT, RETRY_COUNT, UNDO_COUNT, ABANDONED,
                            TOTAL_TOKENS, TOTAL_LATENCY_MS, SCORE_VERSION)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                           ON DUPLICATE KEY UPDATE
                              SUCCESS_SCORE = VALUES(SUCCESS_SCORE),
                              USER_SATISFACTION = VALUES(USER_SATISFACTION),
                              TOOL_ERROR_RATE = VALUES(TOOL_ERROR_RATE),
                              TURN_COUNT = VALUES(TURN_COUNT),
                              TOOL_CALL_COUNT = VALUES(TOOL_CALL_COUNT),
                              RETRY_COUNT = VALUES(RETRY_COUNT),
                              UNDO_COUNT = VALUES(UNDO_COUNT),
                              ABANDONED = VALUES(ABANDONED),
                              COMPUTED_AT = CURRENT_TIMESTAMP""",
                        (
                            score.session_id, agent_name, client_code,
                            score.success_score, score.user_satisfaction,
                            score.tool_error_rate, score.turn_count,
                            score.tool_call_count, score.retry_count,
                            score.undo_count, 1 if score.abandoned else 0,
                            score.total_tokens, score.total_latency_ms,
                            SCORE_VERSION,
                        ),
                    )
        except Exception as e:
            logger.error("Failed to persist score for %s: %s", score.session_id, e)


# Singleton
_outcome_analyzer: Optional[OutcomeAnalyzer] = None


def get_outcome_analyzer() -> OutcomeAnalyzer:
    global _outcome_analyzer
    if _outcome_analyzer is None:
        _outcome_analyzer = OutcomeAnalyzer()
    return _outcome_analyzer
