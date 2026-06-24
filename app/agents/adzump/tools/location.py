"""Real-estate location confirmation tool.

Gated on real-estate business-type keywords. Emits BOTH a prompt text and
the map widget atomically — the LLM no longer has to remember to write
"I'll show you a map" alongside the tool call. Frontend handles geocoding,
rendering, pin-drag. On confirm it sends coords back as JSON and the LLM
stores them via `set_campaign_spec`.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.core.tools.base import ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_REAL_ESTATE_KEYWORDS = (
    "real estate",
    "realty",
    "villa",
    "apartment",
    "residential",
    "property",
    "housing",
    "homes",
    "realtor",
    "township",
    "builder",
    "developer",
)


def _is_real_estate(business_type: str) -> bool:
    bt = (business_type or "").strip().lower()
    return any(kw in bt for kw in _REAL_ESTATE_KEYWORDS)


def _detected_location(product_data: dict) -> str:
    loc = product_data.get("location") or {}
    if isinstance(loc, str):
        return loc.strip()
    if isinstance(loc, dict):
        return (loc.get("location") or "").strip()
    return ""


async def _confirm_location(params: dict, context: dict) -> ToolResult:
    session_ctx = context.get("session_context") or {}
    product = session_ctx.get("product_data") or {}
    business_type = (product.get("business_type") or "").strip()

    if not _is_real_estate(business_type):
        logger.info("confirm_location skipped: business_type=%r not real-estate", business_type)
        return ToolResult(
            success=False,
            error=f"confirm_location only applies to real-estate campaigns — business_type is '{business_type}'. Skip this step.",
        )

    detected = _detected_location(product)
    product_name = (product.get("product_name") or "").strip()
    display = f"{product_name}, {detected}" if product_name and detected else (detected or product_name)

    payload = {
        "location": detected,
        "product_location": display,
        "product_name": product_name,
        "query": display,
        "api_key": settings.GOOGLE_MAPS_API_KEY,
        "location_found": bool(detected),
    }

    prompt = (params.get("prompt") or "").strip()
    if not prompt:
        if display:
            prompt = (
                f"I've detected the location as **{display}**. Please confirm "
                f"on the map below — drag the pin if it's off."
            )
        else:
            prompt = "Please confirm the campaign location on the map below — drag the pin if it's off."

    stream = context.get("event_stream")
    if stream is not None:
        await stream.emit_text(f"\n\n{prompt}\n")
        await stream.emit_data("location_map", payload)

    session_ctx["_pending_location_confirm"] = detected

    return ToolResult(
        success=True,
        data={"location": detected, "shown": True},
        summary=(
            f"Map + prompt shown for '{display or 'unknown location'}'. The prompt "
            "is already on screen — do NOT restate or paraphrase it. Wait for the "
            "user's reply."
        ),
    )


confirm_location = ToolDefinition(
    name="confirm_location",
    description=(
        "Ask the user to confirm or correct the project location on a map. "
        "Real-estate campaigns only — refuses for other business types. The "
        "tool emits the prompt text AND the map widget itself — your response "
        "must contain NO free text. Takes no parameters; reads the detected "
        "location from product_data."
    ),
    display_name="Confirm Location",
    parameters=[],
    execute=_confirm_location,
    # v8 Plan B WS3 · deferred elicitation. After this returns, the run loop
    # breaks and yields the turn to the user; their reply resumes next turn.
    kind="elicitation",
    elicit_mode="deferred",
    elicit_expects="single",
)

LOCATION_TOOLS = [confirm_location]
