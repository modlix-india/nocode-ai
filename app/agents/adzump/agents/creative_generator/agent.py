"""CreativeSelectionAgent definition for background image selection."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List, Dict

from app.services.llm_provider import get_llm_provider

logger = logging.getLogger(__name__)

SELECTION_PROVIDER = "openai"
SELECTION_MODEL_TIER = "fast"
SELECTION_MAX_TOKENS = 600


class CreativeSelectionAgent:
    """Vision-based singleton that selects the best base image from candidate assets."""

    _instance: CreativeSelectionAgent | None = None
    _system_prompt: str = ""

    def __init__(self) -> None:
        prompts_dir = Path(__file__).resolve().parent / "prompts"
        self._system_prompt = (prompts_dir / "creative_selection.txt").read_text(encoding="utf-8")

    @classmethod
    def get_instance(cls) -> CreativeSelectionAgent:
        if cls._instance is None:
            cls._instance = cls()
            logger.info("CreativeSelectionAgent created (single-shot vision, no tools)")
        return cls._instance

    async def select(
        self,
        user_message: str,
        image_blocks: List[Dict[str, Any]],
        parent_event_stream=None,
        auth=None,
        parent_session_context: dict | None = None,
    ) -> str:
        """Call the LLM directly to pick the best image (no sub-agent streaming)."""
        provider = get_llm_provider(SELECTION_PROVIDER)

        user_content: list[dict[str, Any]] = []
        for b64, mime in image_blocks:
            user_content.append(provider.format_image_content(b64, mime))
        user_content.append({"type": "text", "text": user_message})

        response = await provider.create_completion(
            system_prompt=self._system_prompt,
            messages=[{"role": "user", "content": user_content}],
            model_tier=SELECTION_MODEL_TIER,
            max_tokens=SELECTION_MAX_TOKENS,
        )
        return (response.get("content") or "").strip()


def get_creative_selection_agent() -> CreativeSelectionAgent:
    return CreativeSelectionAgent.get_instance()
