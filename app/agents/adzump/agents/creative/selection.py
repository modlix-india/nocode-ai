from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List

from app.services.llm_provider import get_llm_provider
from app.agents.adzump.agents.creative.tools import _download_image

logger = logging.getLogger(__name__)

SELECTION_MODEL_TIER = "fast"
SELECTION_MAX_TOKENS = 600


class CreativeSelectionAgent:
    """Vision-based helper that selects the best base image from candidate assets."""

    _instance: CreativeSelectionAgent | None = None
    _system_prompt: str = ""

    def __init__(self) -> None:
        prompts_dir = Path(__file__).resolve().parent / "prompts"
        self._system_prompt = (prompts_dir / "creative_selection.txt").read_text(
            encoding="utf-8"
        )

    @classmethod
    def get_instance(cls) -> CreativeSelectionAgent:
        if cls._instance is None:
            cls._instance = cls()
            logger.info("CreativeSelectionAgent created (single-shot vision)")
        return cls._instance

    async def select(
        self,
        user_message: str,
        image_blocks: List[tuple[str, str]],
        provider_name: str = "openai",
    ) -> str:
        """Call the LLM directly to pick the best image."""
        provider = get_llm_provider(provider_name)

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


async def select_best_image(
    pool: list[str],
    business_type: str,
    context: dict,
    provider_name: str = "openai",
) -> str | None:
    """Use the CreativeSelectionAgent to choose the single best base image from candidate pool."""
    if not pool:
        return None

    # Limit evaluation to the first 10 candidates to avoid missing good images
    candidates = pool[:10]
    from app.agents.adzump._shared import emit_progress

    await emit_progress(
        context, "Selecting and verifying the best candidate from asset images..."
    )

    candidate_parts = []
    downloaded_paths = []
    for path in candidates:
        try:
            image_bytes, mime_type = await _download_image(path)
            b64 = base64.b64encode(image_bytes).decode("utf-8")
            candidate_parts.append((b64, mime_type))
            downloaded_paths.append(path)
        except Exception as e:
            logger.warning("Failed to download candidate image from %s: %s", path, e)

    if not candidate_parts:
        return None

    try:
        selection_agent = get_creative_selection_agent()

        user_msg = (
            f"Candidate background images for a Facebook ad creative of a {business_type} brand. "
            "Evaluate according to the system prompt criteria and return JSON."
        )

        completion = await selection_agent.select(
            user_message=user_msg,
            image_blocks=candidate_parts,
            provider_name=provider_name,
        )

        choice_data = _extract_json(completion)
        if choice_data:
            idx = choice_data.get("selected_index", -1)
            if idx == -1:
                logger.info("Selection agent rejected all candidates as unsuitable.")
                return None
            if 0 <= idx < len(downloaded_paths):
                return downloaded_paths[idx]
    except Exception as e:
        logger.warning("Selection agent failed. Error: %s", e)

    return None


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    fence_re = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)
    m = fence_re.search(text)
    raw = m.group(1) if m else text.strip()
    raw = re.sub(r"^```[a-z]*\s*", "", raw)
    raw = re.sub(r"\s*```\s*$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        for open_c, close_c in (("{", "}"), ("[", "]")):
            s = raw.find(open_c)
            e = raw.rfind(close_c)
            if s != -1 and e != -1 and e > s:
                try:
                    return json.loads(raw[s : e + 1])
                except json.JSONDecodeError:
                    continue
    return None
