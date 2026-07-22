"""Single-call MiniMax M3 helper for the A3 roles.

The planner / critic / repair steps are single-shot reasoning calls (not tool
loops), so they bypass the BaseAgent loop and hit the provider directly: one
system prompt, one user message, one JSON object back. Provider + tier resolve
the same way as the Adzump2 chat agent (``ADZUMP2_PROVIDER`` -> MiniMax M3,
``AGENT_MODEL_TIER``). Kept in one place so the three roles stay identical in
plumbing and only differ in prompt.

Offline note: this module makes no network call at import; ``get_llm_provider``
is resolved lazily inside ``complete_json``. Tests inject scripted roles and
never reach here.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.adzump._shared import extract_json
from app.config import settings

logger = logging.getLogger(__name__)


def _provider_name() -> str:
    return getattr(settings, "ADZUMP2_PROVIDER", None) or settings.LLM_PROVIDER


async def complete_json(
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int = 4096,
    log_tag: str = "adzump2.planner",
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Run one M3 completion and pull the first JSON object out of it.

    Returns ``(parsed_json_or_None, usage)``. A parse miss returns ``None`` — the
    caller decides the fail-safe (empty patch / zero-score critique), never a
    silent success.
    """
    # Lazy import: the LLM SDK graph must not load for offline callers/tests.
    from app.services.llm_provider import get_llm_provider

    provider = get_llm_provider(_provider_name())
    try:
        resp = await provider.create_completion(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            model_tier=settings.AGENT_MODEL_TIER,
            max_tokens=max_tokens,
        )
    except Exception as e:  # provider/transport failure — surface as a parse miss
        logger.warning("%s: LLM call failed: %s: %s", log_tag, type(e).__name__, e)
        return None, {}

    content = resp.get("content") if isinstance(resp, dict) else None
    usage = (resp.get("usage") if isinstance(resp, dict) else None) or {}
    if not isinstance(content, str) or not content.strip():
        logger.warning("%s: empty completion content", log_tag)
        return None, usage
    parsed = extract_json(content)
    if parsed is None:
        logger.warning("%s: no JSON object in completion (%d chars)", log_tag, len(content))
    return parsed, usage
