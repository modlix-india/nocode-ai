"""Knowledge extractor - mines successful sessions for reusable patterns.

Two extraction strategies:
1. Rule-based: Detect common tool sequences from high-scoring sessions
2. Error-to-pitfall: Promote frequent tool errors into prompt-injectable pitfalls

Knowledge is stored in MySQL ai_learning_knowledge with FULLTEXT search.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional, List

from app.db.connection import get_connection, is_pool_available
from app.learning.models import KnowledgeEntry, KnowledgeType, KnowledgeStatus

logger = logging.getLogger(__name__)

# Maximum knowledge entries injected per request
MAX_INJECTION_ENTRIES = 5


class KnowledgeExtractor:
    """Extracts, stores, and retrieves knowledge entries."""

    # ── Extraction ───────────────────────────────────────────────

    async def extract_patterns_from_session(
        self, session_id: str, agent_name: str
    ) -> List[KnowledgeEntry]:
        """Extract tool sequence patterns from a high-scoring session.

        Only call this for sessions with success_score > 0.7.
        Creates a PATTERN knowledge entry from the tool call sequence.
        """
        if not is_pool_available():
            return []

        entries: List[KnowledgeEntry] = []
        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """SELECT TURN_NUMBER, USER_INSTRUCTION,
                                  ASSISTANT_SUMMARY, TOOL_CALLS_JSON
                           FROM ai_session_history
                           WHERE SESSION_ID = %s AND TOOL_CALLS_JSON IS NOT NULL
                           ORDER BY TURN_NUMBER""",
                        (session_id,),
                    )
                    turns = await cursor.fetchall()

            if not turns:
                return []

            # Build compact tool sequence from successful calls
            tool_sequence: List[str] = []
            for turn in turns:
                try:
                    calls = json.loads(turn[3])
                    for call in calls:
                        if call.get("success"):
                            tool_sequence.append(call["tool"])
                except (json.JSONDecodeError, TypeError, KeyError):
                    pass

            if len(tool_sequence) < 3:
                return []  # Too short to be a useful pattern

            category = self._categorize_sequence(tool_sequence)
            first_instruction = (turns[0][1] or "Unknown task")[:100]
            title = f"Successful {category}: {first_instruction}"
            content = self._format_pattern_content(turns, tool_sequence)

            entry_id = await self._store_knowledge(
                knowledge_type=KnowledgeType.PATTERN,
                agent_name=agent_name,
                category=category,
                title=title,
                content=content,
                source_session_ids=session_id,
                tool_sequence_json=json.dumps(tool_sequence),
            )

            if entry_id:
                entry = KnowledgeEntry(
                    id=entry_id,
                    knowledge_type=KnowledgeType.PATTERN,
                    agent_name=agent_name,
                    category=category,
                    title=title,
                    content=content,
                    tool_sequence_json=json.dumps(tool_sequence),
                )
                entries.append(entry)

        except Exception as e:
            logger.error("Failed to extract patterns from %s: %s", session_id, e)

        return entries

    async def extract_pitfall_from_errors(
        self, agent_name: str, tool_name: str, error_message: str,
        tool_input: dict,
    ) -> None:
        """Record a tool error pattern for pitfall detection.

        Uses INSERT ON DUPLICATE KEY UPDATE to count occurrences.
        Called from BaseAgent._on_tool_error hook.
        """
        if not is_pool_available():
            return

        normalized = self._normalize_error(error_message)
        if not normalized:
            return

        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """INSERT INTO ai_learning_tool_errors
                           (AGENT_NAME, TOOL_NAME, ERROR_PATTERN, EXAMPLE_INPUT_JSON)
                           VALUES (%s, %s, %s, %s)
                           ON DUPLICATE KEY UPDATE
                              OCCURRENCE_COUNT = OCCURRENCE_COUNT + 1,
                              LAST_SEEN_AT = CURRENT_TIMESTAMP,
                              EXAMPLE_INPUT_JSON = VALUES(EXAMPLE_INPUT_JSON)""",
                        (
                            agent_name, tool_name,
                            normalized[:512],
                            json.dumps(tool_input, default=str)[:2000],
                        ),
                    )
        except Exception as e:
            logger.warning("Failed to record tool error: %s", e)

    # ── Retrieval ────────────────────────────────────────────────

    async def get_relevant_knowledge(
        self, agent_name: str, user_message: str,
        max_entries: int = MAX_INJECTION_ENTRIES,
    ) -> List[KnowledgeEntry]:
        """Retrieve knowledge entries relevant to the current user message.

        Strategy:
        1. MySQL FULLTEXT search
        2. Always include active PITFALLs
        """
        entries: List[KnowledgeEntry] = []

        # Strategy 1: MySQL FULLTEXT search
        sql_entries = await self._search_mysql(agent_name, user_message, max_entries)
        entries.extend(sql_entries)

        # Strategy 2: Always include top pitfalls
        pitfalls = await self._get_top_pitfalls(agent_name, limit=2)
        existing_ids = {e.id for e in entries}
        entries.extend(e for e in pitfalls if e.id not in existing_ids)

        return entries[:max_entries]

    async def increment_use_count(self, knowledge_id: int) -> None:
        """Track that a knowledge entry was injected into a prompt."""
        if not is_pool_available():
            return
        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "UPDATE ai_learning_knowledge SET USE_COUNT = USE_COUNT + 1 WHERE ID = %s",
                        (knowledge_id,),
                    )
        except Exception:
            pass

    # ── Private helpers ──────────────────────────────────────────

    def _categorize_sequence(self, tool_sequence: List[str]) -> str:
        """Categorize a tool sequence by the dominant tool type."""
        category_map = {
            "list_pages": "page_management",
            "create_page": "page_creation",
            "read_page": "page_editing",
            "add_component": "component_building",
            "update_component": "component_editing",
            "batch_operations": "batch_editing",
            "create_function": "function_creation",
            "write_event_function": "event_creation",
            "list_themes": "styling",
            "create_theme": "styling",
            "list_applications": "app_management",
            "create_application": "app_creation",
        }
        for tool in tool_sequence:
            if tool in category_map:
                return category_map[tool]
        return "general"

    def _format_pattern_content(self, turns: list, tool_sequence: List[str]) -> str:
        """Format a successful session into injectable knowledge text."""
        lines = ["This pattern was extracted from a successful session:\n"]
        lines.append(f"Tool sequence: {' -> '.join(tool_sequence[:20])}\n")
        for turn in turns[:5]:
            instruction = (turn[1] or "")[:200]
            summary = (turn[2] or "")[:200]
            if instruction:
                lines.append(f"User: {instruction}")
            if summary:
                lines.append(f"Agent: {summary}\n")
        return "\n".join(lines)

    def _normalize_error(self, error: str) -> str:
        """Normalize an error message by stripping variable parts."""
        if not error:
            return ""
        # Strip ObjectIds, UUIDs, timestamps, specific IDs
        error = re.sub(r"[0-9a-f]{24}", "<ID>", error)
        error = re.sub(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            "<UUID>", error,
        )
        error = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", "<TIMESTAMP>", error)
        error = re.sub(r"HTTP \d+:", "HTTP <STATUS>:", error)
        return error.strip()

    async def _store_knowledge(self, **kwargs) -> Optional[int]:
        """Insert a knowledge entry into MySQL."""
        if not is_pool_available():
            return None
        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """INSERT INTO ai_learning_knowledge
                           (KNOWLEDGE_TYPE, AGENT_NAME, CATEGORY, TITLE,
                            CONTENT, SOURCE_SESSION_IDS, TOOL_SEQUENCE_JSON)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (
                            kwargs["knowledge_type"].value,
                            kwargs["agent_name"],
                            kwargs.get("category"),
                            kwargs["title"],
                            kwargs["content"],
                            kwargs.get("source_session_ids"),
                            kwargs.get("tool_sequence_json"),
                        ),
                    )
                    return cursor.lastrowid
        except Exception as e:
            logger.error("Failed to store knowledge entry: %s", e)
            return None

    async def _search_mysql(
        self, agent_name: str, query: str, limit: int,
    ) -> List[KnowledgeEntry]:
        """MySQL FULLTEXT search fallback."""
        if not is_pool_available():
            return []
        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """SELECT ID, KNOWLEDGE_TYPE, AGENT_NAME, CATEGORY,
                                  TITLE, CONTENT, TOOL_SEQUENCE_JSON,
                                  RELEVANCE_SCORE, USE_COUNT, STATUS
                           FROM ai_learning_knowledge
                           WHERE AGENT_NAME = %s AND STATUS = 'ACTIVE'
                             AND MATCH(TITLE, CONTENT) AGAINST(%s IN NATURAL LANGUAGE MODE)
                           ORDER BY RELEVANCE_SCORE DESC
                           LIMIT %s""",
                        (agent_name, query, limit),
                    )
                    return [self._row_to_entry(row) for row in await cursor.fetchall()]
        except Exception as e:
            logger.debug("MySQL fulltext search failed: %s", e)
            return []

    async def _get_top_pitfalls(self, agent_name: str, limit: int = 2) -> List[KnowledgeEntry]:
        """Get top active pitfall entries for the agent."""
        if not is_pool_available():
            return []
        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """SELECT ID, KNOWLEDGE_TYPE, AGENT_NAME, CATEGORY,
                                  TITLE, CONTENT, TOOL_SEQUENCE_JSON,
                                  RELEVANCE_SCORE, USE_COUNT, STATUS
                           FROM ai_learning_knowledge
                           WHERE AGENT_NAME = %s AND STATUS = 'ACTIVE'
                             AND KNOWLEDGE_TYPE = 'PITFALL'
                           ORDER BY RELEVANCE_SCORE DESC
                           LIMIT %s""",
                        (agent_name, limit),
                    )
                    return [self._row_to_entry(row) for row in await cursor.fetchall()]
        except Exception as e:
            logger.debug("Failed to get pitfalls: %s", e)
            return []

    async def _load_entry_by_id(self, entry_id: int) -> Optional[KnowledgeEntry]:
        """Load a single knowledge entry by ID."""
        if not is_pool_available():
            return None
        try:
            async with get_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """SELECT ID, KNOWLEDGE_TYPE, AGENT_NAME, CATEGORY,
                                  TITLE, CONTENT, TOOL_SEQUENCE_JSON,
                                  RELEVANCE_SCORE, USE_COUNT, STATUS
                           FROM ai_learning_knowledge WHERE ID = %s""",
                        (entry_id,),
                    )
                    row = await cursor.fetchone()
                    return self._row_to_entry(row) if row else None
        except Exception:
            return None

    def _row_to_entry(self, row: tuple) -> KnowledgeEntry:
        return KnowledgeEntry(
            id=row[0],
            knowledge_type=KnowledgeType(row[1]),
            agent_name=row[2],
            category=row[3],
            title=row[4],
            content=row[5],
            tool_sequence_json=row[6],
            relevance_score=row[7] or 1.0,
            use_count=row[8] or 0,
            status=KnowledgeStatus(row[9]) if row[9] else KnowledgeStatus.ACTIVE,
        )


# Singleton
_knowledge_extractor: Optional[KnowledgeExtractor] = None


def get_knowledge_extractor() -> KnowledgeExtractor:
    global _knowledge_extractor
    if _knowledge_extractor is None:
        _knowledge_extractor = KnowledgeExtractor()
    return _knowledge_extractor
