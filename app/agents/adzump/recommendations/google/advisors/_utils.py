"""Shared utility functions for recommendation advisors."""
import json
import logging
import uuid
from typing import Any

from app.core.session import BaseSession

logger = logging.getLogger(__name__)


def clean_and_load_json(content: str) -> Any:
    content = content.strip()
    if content.startswith("```"):
        first_newline = content.find("\n")
        if first_newline != -1:
            first_line = content[:first_newline].strip()
            if first_line.startswith("```"):
                content = content[first_newline:].strip()
        if content.endswith("```"):
            content = content[:-3].strip()
    return json.loads(content)


# Shared telemetry and billing bridge for LLM-powered sub-advisors.
# This ensures that any LLM tokens consumed by lower-level, non-agent modules 
# are correctly recorded under the active session and multi-tenant database tables.
# Accepts an optional BaseSession parameter to seamlessly support both user-facing chat 
# sessions and background virtual scheduler runner sessions.
async def track_advisor_llm_call(
    session: BaseSession | None,
    response: dict[str, Any],
    prefix: str,
    provider_name: str = "openai",
) -> None:
    """Record token usage of an advisor LLM call to the session and DB."""
    if not session or not response:
        return
    usage = response.get("usage")
    if not usage:
        return
    model = response.get("model", "unknown")
    request_id = response.get("request_id") or f"{prefix}_sub_{uuid.uuid4().hex[:8]}"

    # 1. Accumulate tokens in memory
    session.accumulate_usage(usage)

    # 2. Record to DB (async)
    try:
        await session.record_token_usage(
            usage=usage,
            request_id=request_id,
            model=model,
            provider_name=provider_name,
        )
    except Exception as e:
        logger.warning(
            "Failed to record advisor token usage to database: %s",
            e,
            exc_info=True,
        )
