"""Location confirmation and geo-targeting tools for Adzump.

Provides tools for map validation of business locations, platform-specific
geo-targeting discovery, and manual targeting edits (additions/deletions).
"""

from __future__ import annotations

import logging

from app.config import settings
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump._shared import product_location_str as _detected_location
from app.agents.adzump.tools.campaign_data import is_real_estate
from app.agents.adzump.agents.location.agent import get_location_agent

logger = logging.getLogger(__name__)


async def _confirm_location(params: dict, context: dict) -> ToolResult:
    session_ctx = context.get("session_context") or {}
    product = session_ctx.get("product_data") or {}
    business_type = (product.get("business_type") or "").strip()

    if not is_real_estate(business_type):
        logger.info(
            "confirm_location skipped: business_type=%r is not real estate", business_type
        )
        return ToolResult(
            success=False,
            error=(
                f"confirm_location only applies to real estate businesses. "
                f"Business type is '{business_type}'. Skip this step."
            ),
        )

    detected = _detected_location(product)
    product_name = (product.get("product_name") or "").strip()
    display = (
        f"{product_name}, {detected}"
        if product_name and detected
        else (detected or product_name)
    )

    payload = {
        "location": detected,
        "product_location": display,
        "product_name": product_name,
        "query": display,
        "api_key": settings.GOOGLE_MAPS_API_KEY,
        "location_found": bool(detected),
    }

    # The tool declares no parameters (see ToolDefinition below) - the prompt
    # is always built here, never model-supplied.
    if display:
        prompt = (
            f"I've detected the location as **{display}**. Please confirm "
            f"on the map below - drag the pin if it's off."
        )
    else:
        prompt = "Please confirm the campaign location on the map below - drag the pin if it's off."

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
            "is already on screen - do NOT restate or paraphrase it. Wait for the "
            "user's reply."
        ),
    )


async def _manage_targeting_locations(params: dict, context: dict) -> ToolResult:
    """Orchestrator-facing entry point for geo targeting.

    The orchestrator (Adzump) is a pure router - it does NOT decide which
    action to take (discover / add / delete) or extract its parameters. It
    just routes "this is a geo targeting request" here with the user's
    verbatim message. ``LocationAgent.handle()`` runs the location agent's
    own tool-use loop, which acts by picking ONE tool (discovery or edit).

    This keeps the orchestrator from growing a hallucination surface around
    geo-targeting domain knowledge as the system scales.
    """
    user_message = (params.get("user_message") or "").strip()
    if not user_message:
        return ToolResult(
            success=False,
            error=(
                "manage_targeting_locations requires a `user_message` - the "
                "orchestrator should forward the user's verbatim text. "
                "Empty messages are rejected here so the orchestrator gets "
                "a clean retry signal."
            ),
        )
    location_agent = get_location_agent()
    return await location_agent.handle(user_message, context)


confirm_location = ToolDefinition(
    name="confirm_location",
    description=(
        "Ask the user to confirm or correct the business location on a map. "
        "Only applies to local physical businesses. Emits the prompt text "
        "and map widget atomically. Reads location from product_data."
    ),
    display_name="Confirm Location",
    parameters=[],
    execute=_confirm_location,
    kind="elicitation",
    elicit_mode="deferred",
    elicit_expects="single",
)

manage_targeting_locations = ToolDefinition(
    name="manage_targeting_locations",
    description=(
        "Make any change to the campaign's geo targeting - initial setup, "
        "adding an area, or removing one. Always pass the user's verbatim "
        "message via `user_message`; do NOT try to classify it yourself. "
        "The geo-targeting subsystem interprets the intent (discover / "
        "add / delete), extracts parameters, and dispatches internally. "
        "You only route here when the user wants a change to targeting."
    ),
    display_name="Geo Targeting",
    parameters=[
        ToolParameter(
            name="user_message",
            type="string",
            description=(
                "The user's verbatim message about geo targeting. Examples: "
                "'set targeting for Bangalore', 'add Mumbai', 'delete the "
                "second area', 'remove that one I just added'. The subsystem "
                "interprets intent + extracts parameters - do NOT pre-classify."
            ),
            required=True,
        ),
        # NOTE: platform-handle params (resourceName / key / type /
        # place_id) and structured action params (action / index / lat / lng
        # / etc.) are deliberately NOT exposed here. They belong to the
        # LocationAgent's own loop (see agents/location/agent.py handle()),
        # which extracts them from the user's natural-language message when
        # picking its tool. Exposing them here would let the orchestrator LLM
        # fabricate structured parameters with no traceability check.
    ],
    execute=_manage_targeting_locations,
)

LOCATION_TOOLS = [confirm_location, manage_targeting_locations]
