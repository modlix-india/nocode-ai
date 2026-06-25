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
from app.agents.adzump.agents.geo.agent import get_geo_targeting_agent

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


async def _discover_geo_targets(params: dict, context: dict) -> ToolResult:
    return await get_geo_targeting_agent().discover(params, context)


async def _modify_targeting_location(params: dict, context: dict) -> ToolResult:
    return await get_geo_targeting_agent().modify(params, context)


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

discover_geo_targets = ToolDefinition(
    name="discover_geo_targets",
    description=(
        "Resolve targetable areas/constants for the active ad network (Google Ads or Meta Ads). "
        "For local physical businesses, it scans neighborhoods within a radius. For broad "
        "businesses, it resolves country/region names directly."
    ),
    display_name="Discover Geo Targets",
    parameters=[
        ToolParameter(
            name="location_name",
            type="string",
            description="Target location, city, state, or country name. Optional.",
            required=False,
        ),
    ],
    execute=_discover_geo_targets,
)

modify_targeting_location = ToolDefinition(
    name="modify_targeting_location",
    description=(
        "Add or delete a campaign targeting location. "
        "Expects 1-based index for delete action."
    ),
    display_name="Modify Targeting Location",
    parameters=[
        ToolParameter(
            name="action",
            type="string",
            description="The action to perform: 'add' or 'delete'.",
            required=True,
        ),
        ToolParameter(
            name="index",
            type="integer",
            description="The 1-based index to delete. Required for 'delete' action.",
            required=False,
        ),
        ToolParameter(
            name="name",
            type="string",
            description="The location name. Required for 'add' action.",
            required=False,
        ),
        ToolParameter(
            name="city",
            type="string",
            description="The city name.",
            required=False,
        ),
        ToolParameter(
            name="state",
            type="string",
            description="The state name.",
            required=False,
        ),
        ToolParameter(
            name="pincode",
            type="string",
            description="The pincode/ZIP code.",
            required=False,
        ),
        ToolParameter(
            name="lat",
            type="number",
            description="Latitude coordinates.",
            required=False,
        ),
        ToolParameter(
            name="lng",
            type="number",
            description="Longitude coordinates.",
            required=False,
        ),
        ToolParameter(
            name="radius",
            type="number",
            description="Radial distance in km.",
            required=False,
        ),
        ToolParameter(
            name="google_id",
            type="string",
            description="Google Ads Criteria ID.",
            required=False,
        ),
        ToolParameter(
            name="meta_key",
            type="string",
            description="Meta location key.",
            required=False,
        ),
        ToolParameter(
            name="place_id",
            type="string",
            description="Google Place ID.",
            required=False,
        ),
    ],
    execute=_modify_targeting_location,
)

LOCATION_TOOLS = [confirm_location, discover_geo_targets, modify_targeting_location]
