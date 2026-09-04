"""The one seam for schema-constrained helper LLM calls.

Feature code that needs a small structured judgment (not an agent loop - the
call must run unconditionally, not at a model's discretion) goes through
``structured_call``: provider switch via get_llm_provider, one transport
retry, and a usage log line per call. Never copy the raw-SDK pattern into
feature modules (the deleted shortlist classifier was that anti-pattern).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.services.llm_provider import get_llm_provider

logger = logging.getLogger(__name__)


class StructuredCallError(Exception):
    """The call failed after retry, or the provider returned no parseable
    object. Callers own the fallback policy (there is none here)."""


async def structured_call(
    *,
    task: str,
    system_prompt: str,
    payload: dict,
    output_schema: dict,
    model_tier: str = "fast",
    max_tokens: int = 2048,
) -> dict[str, Any]:
    """One schema-constrained LLM call. ``task`` names the caller in logs.

    Returns the parsed object. Retries ONCE on transport/parse failure, then
    raises StructuredCallError - semantic validation (row counts, ID echoes)
    belongs to the caller, which sees the parsed object.
    """
    payload_json = json.dumps(payload, ensure_ascii=False)
    provider = get_llm_provider()
    last_error: Exception | None = None
    for attempt in (1, 2):
        started = time.monotonic()
        try:
            response = await provider.create_structured_completion(
                system_prompt=system_prompt,
                payload_json=payload_json,
                output_schema=output_schema,
                model_tier=model_tier,
                max_tokens=max_tokens,
            )
            parsed = response.get("parsed")
            if not isinstance(parsed, dict):
                raise StructuredCallError("provider returned no parsed object")
            usage = response.get("usage") or {}
            logger.info(
                "structured_call: task=%s model=%s in=%s out=%s ms=%d attempt=%d",
                task, response.get("model"), usage.get("input_tokens"),
                usage.get("output_tokens"),
                int((time.monotonic() - started) * 1000), attempt,
            )
            return parsed
        except NotImplementedError:
            raise
        except Exception as e:
            last_error = e
            logger.warning(
                "structured_call_attempt_failed: task=%s attempt=%d %s: %s",
                task, attempt, type(e).__name__, str(e)[:200],
            )
    raise StructuredCallError(
        f"structured call '{task}' failed after retry: "
        f"{type(last_error).__name__}: {str(last_error)[:200]}"
    )
