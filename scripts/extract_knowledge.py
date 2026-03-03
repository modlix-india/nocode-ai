"""Batch script — extract knowledge from high-scoring sessions.

Queries completed sessions with success_score > 0.7 that haven't
been extracted yet, and creates PATTERN knowledge entries.

Usage:
    python scripts/extract_knowledge.py [--min-score 0.7] [--limit 50]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path so app imports work
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import settings, initialize_settings
from app.db.connection import init_db_pool, close_db_pool, get_connection, is_pool_available
from app.learning.knowledge import get_knowledge_extractor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def get_extractable_sessions(min_score: float, limit: int) -> list[dict]:
    """Find high-scoring sessions not yet extracted into knowledge."""
    if not is_pool_available():
        return []

    async with get_connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """SELECT s.SESSION_ID, s.AGENT_NAME, s.SUCCESS_SCORE
                   FROM ai_learning_session_scores s
                   WHERE s.SUCCESS_SCORE >= %s
                     AND s.SESSION_ID NOT IN (
                         SELECT DISTINCT SUBSTRING_INDEX(SOURCE_SESSION_IDS, ',', 1)
                         FROM ai_learning_knowledge
                         WHERE SOURCE_SESSION_IDS IS NOT NULL
                     )
                   ORDER BY s.SUCCESS_SCORE DESC
                   LIMIT %s""",
                (min_score, limit),
            )
            rows = await cursor.fetchall()
            return [
                {"session_id": r[0], "agent_name": r[1] or "appbuilder", "score": r[2]}
                for r in rows
            ]


async def main(min_score: float, limit: int) -> None:
    logger.info("Initializing settings...")
    await initialize_settings()

    if not settings.AI_TRACKING_ENABLED:
        logger.error("AI_TRACKING_ENABLED is False — no database configured")
        return

    logger.info("Connecting to database...")
    await init_db_pool()

    try:
        sessions = await get_extractable_sessions(min_score, limit)
        logger.info(
            "Found %d sessions with score >= %.2f to extract",
            len(sessions), min_score,
        )

        extractor = get_knowledge_extractor()
        extracted = 0

        for sess in sessions:
            logger.info(
                "Extracting from %s (score=%.3f, agent=%s)",
                sess["session_id"], sess["score"], sess["agent_name"],
            )
            entries = await extractor.extract_patterns_from_session(
                session_id=sess["session_id"],
                agent_name=sess["agent_name"],
            )
            if entries:
                extracted += len(entries)
                for entry in entries:
                    logger.info("  Created: [%s] %s", entry.knowledge_type.value, entry.title)
            else:
                logger.info("  No patterns extracted (too few tool calls)")

        logger.info("Done. Extracted %d knowledge entries from %d sessions.", extracted, len(sessions))

    finally:
        await close_db_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract knowledge from high-scoring sessions")
    parser.add_argument("--min-score", type=float, default=0.7, help="Minimum success_score (default: 0.7)")
    parser.add_argument("--limit", type=int, default=50, help="Max sessions to process (default: 50)")
    args = parser.parse_args()

    asyncio.run(main(args.min_score, args.limit))
