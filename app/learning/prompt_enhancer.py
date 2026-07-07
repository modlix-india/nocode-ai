"""Prompt enhancer — injects learned knowledge into system prompts.

Design principles:
- Hard token budget: MAX_ENHANCEMENT_TOKENS (2000 tokens ~ 8000 chars)
- Priority-based: pitfalls > examples > patterns > lessons
- Appends as dynamic context — does NOT modify static docs (preserves prompt caching)
"""

from __future__ import annotations

import logging
from typing import List, Optional

from app.learning.models import PromptPatch, KnowledgeEntry
from app.learning.knowledge import get_knowledge_extractor

logger = logging.getLogger(__name__)

MAX_ENHANCEMENT_TOKENS = 2000
CHARS_PER_TOKEN = 4


class PromptEnhancer:
    """Builds dynamic prompt patches from the knowledge base."""

    async def build_enhancement(
        self,
        agent_name: str,
        user_message: str,
        session_context: dict,
    ) -> str:
        """Build a prompt enhancement string for the current request.

        Called once per user turn. Returns text to append to the
        dynamic context block in the system prompt.

        Args:
            agent_name: Name of the agent (e.g. "appbuilder").
            user_message: The user's current message (for relevance matching).
            session_context: Session context dict.

        Returns:
            Enhancement text string (empty if no relevant knowledge).
        """
        extractor = get_knowledge_extractor()

        try:
            entries = await extractor.get_relevant_knowledge(
                agent_name=agent_name,
                user_message=user_message,
                max_entries=5,
            )
        except Exception as e:
            logger.warning("Failed to get knowledge entries: %s", e)
            return ""

        if not entries:
            return ""

        patches = self._entries_to_patches(entries)
        text = self._assemble_patches(patches)

        # Track usage for each injected entry
        for entry in entries:
            try:
                await extractor.increment_use_count(entry.id)
            except Exception:
                pass

        return text

    def _entries_to_patches(self, entries: List[KnowledgeEntry]) -> List[PromptPatch]:
        """Convert knowledge entries to prioritized prompt patches."""
        priority_map = {
            "PITFALL": 3.0,
            "EXAMPLE": 2.0,
            "PATTERN": 1.5,
            "LESSON": 1.0,
        }

        patches: List[PromptPatch] = []
        for entry in entries:
            content = self._format_entry(entry)
            token_estimate = len(content) // CHARS_PER_TOKEN

            patches.append(PromptPatch(
                source=entry.knowledge_type.value.lower(),
                content=content,
                priority=priority_map.get(entry.knowledge_type.value, 1.0) * entry.relevance_score,
                token_estimate=token_estimate,
            ))

        patches.sort(key=lambda p: p.priority, reverse=True)
        return patches

    def _assemble_patches(self, patches: List[PromptPatch]) -> str:
        """Assemble patches within the token budget."""
        if not patches:
            return ""

        parts = ["## Learned Knowledge (from past sessions)\n"]
        remaining_tokens = MAX_ENHANCEMENT_TOKENS
        count = 0

        for patch in patches:
            if patch.token_estimate > remaining_tokens:
                continue
            parts.append(patch.content)
            remaining_tokens -= patch.token_estimate
            count += 1

        if count == 0:
            return ""

        result = "\n\n".join(parts)
        logger.debug(
            "Prompt enhancement: %d entries, ~%d tokens",
            count, MAX_ENHANCEMENT_TOKENS - remaining_tokens,
        )
        return result

    def _format_entry(self, entry: KnowledgeEntry) -> str:
        """Format a knowledge entry for prompt injection."""
        header = {
            "PITFALL": "WARNING (known pitfall)",
            "EXAMPLE": "Example from a successful session",
            "PATTERN": "Successful pattern",
            "LESSON": "Lesson learned",
        }.get(entry.knowledge_type.value, "Note")

        lines = [f"### {header}: {entry.title}"]
        content = entry.content
        if len(content) > 1500:
            content = content[:1500] + "..."
        lines.append(content)
        return "\n".join(lines)


# Singleton
_prompt_enhancer: Optional[PromptEnhancer] = None


def get_prompt_enhancer() -> PromptEnhancer:
    global _prompt_enhancer
    if _prompt_enhancer is None:
        _prompt_enhancer = PromptEnhancer()
    return _prompt_enhancer
