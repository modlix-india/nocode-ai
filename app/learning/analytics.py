"""Analytics service — aggregates learning loop metrics.

Provides data for dashboards: daily/weekly/monthly trends,
top error patterns, knowledge base health.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.db.connection import get_connection, is_pool_available
from app.learning.models import AnalyticsSummary

logger = logging.getLogger(__name__)


class LearningAnalytics:
    """Aggregates metrics across the learning loop."""

    async def get_summary(
        self, agent_name: str, period: str = "week",
    ) -> Optional[AnalyticsSummary]:
        """Get aggregate analytics for a time period.

        Args:
            agent_name: Agent to filter by.
            period: "day", "week", or "month".

        Returns:
            AnalyticsSummary or None if DB unavailable.
        """
        if not is_pool_available():
            return None

        interval = {"day": 1, "week": 7, "month": 30}.get(period, 7)

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    # Session score metrics
                    await cursor.execute(
                        """SELECT COUNT(*),
                                  AVG(SUCCESS_SCORE),
                                  AVG(USER_SATISFACTION),
                                  AVG(TOOL_ERROR_RATE)
                           FROM ai_learning_session_scores
                           WHERE AGENT_NAME = %s
                             AND COMPUTED_AT >= DATE_SUB(NOW(), INTERVAL %s DAY)""",
                        (agent_name, interval),
                    )
                    row = await cursor.fetchone()
                    total_sessions = row[0] or 0
                    avg_success = float(row[1]) if row[1] else None
                    avg_satisfaction = float(row[2]) if row[2] else None
                    avg_error_rate = float(row[3]) if row[3] else None

                    # Feedback metrics
                    await cursor.execute(
                        """SELECT COUNT(*),
                                  SUM(CASE WHEN RATING > 0 THEN 1 ELSE 0 END)
                           FROM ai_learning_feedback
                           WHERE AGENT_NAME = %s
                             AND CREATED_AT >= DATE_SUB(NOW(), INTERVAL %s DAY)""",
                        (agent_name, interval),
                    )
                    fb_row = await cursor.fetchone()
                    total_fb = fb_row[0] or 0
                    positive_fb = fb_row[1] or 0
                    positive_pct = (positive_fb / total_fb * 100) if total_fb > 0 else None

                    # Top failing tools
                    await cursor.execute(
                        """SELECT TOOL_NAME, SUM(OCCURRENCE_COUNT) as total
                           FROM ai_learning_tool_errors
                           WHERE AGENT_NAME = %s AND STATUS = 'ACTIVE'
                           GROUP BY TOOL_NAME
                           ORDER BY total DESC LIMIT 5""",
                        (agent_name,),
                    )
                    top_tools = [
                        {"tool": r[0], "error_count": r[1]}
                        for r in await cursor.fetchall()
                    ]

                    # Top error patterns
                    await cursor.execute(
                        """SELECT TOOL_NAME, ERROR_PATTERN, OCCURRENCE_COUNT
                           FROM ai_learning_tool_errors
                           WHERE AGENT_NAME = %s AND STATUS = 'ACTIVE'
                           ORDER BY OCCURRENCE_COUNT DESC LIMIT 5""",
                        (agent_name,),
                    )
                    top_errors = [
                        {"tool": r[0], "pattern": r[1], "count": r[2]}
                        for r in await cursor.fetchall()
                    ]

            return AnalyticsSummary(
                period=period,
                total_sessions=total_sessions,
                avg_success_score=round(avg_success, 4) if avg_success else None,
                avg_user_satisfaction=round(avg_satisfaction, 4) if avg_satisfaction else None,
                avg_tool_error_rate=round(avg_error_rate, 4) if avg_error_rate else None,
                top_failing_tools=top_tools,
                top_error_patterns=top_errors,
                sessions_with_feedback=total_fb,
                positive_feedback_pct=round(positive_pct, 1) if positive_pct else None,
            )
        except Exception as e:
            logger.error("Failed to get analytics summary: %s", e)
            return None


# Singleton
_analytics: Optional[LearningAnalytics] = None


def get_learning_analytics() -> LearningAnalytics:
    global _analytics
    if _analytics is None:
        _analytics = LearningAnalytics()
    return _analytics
