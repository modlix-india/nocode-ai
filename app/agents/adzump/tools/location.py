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
from app.agents.adzump.agents.location.agent import get_geo_targeting_service

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


async def _manage_targeting_locations(params: dict, context: dict) -> ToolResult:
    """Dispatch a single targeting-area operation to the GeoTargetingService.

    Routes by ``params["action"]``:
      * ``discover`` — resolve + map target areas for the active platform.
      * ``add`` — append a target area and re-map.
      * ``delete`` — remove a target area by 1-based index and re-map.

    Any other value returns a structured error without touching the service,
    so the LLM gets a clean retry signal instead of an exception.
    """
    action = params.get("action")
    if action not in ("discover", "add", "delete"):
        return ToolResult(
            success=False,
            error=(
                f"Invalid action: {action!r}. "
                "Expected 'discover', 'add', or 'delete'."
            ),
        )
    service = get_geo_targeting_service()
    if action == "discover":
        return await service.discover(params, context)
    return await service.modify(params, context)


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
        "Discover, add, or delete campaign targeting areas for the active ad network "
        "(Google Ads or Meta Ads). Use 'discover' on first call or after the location "
        "changes; 'add' and 'delete' edit the existing list (delete takes a 1-based index)."
    ),
    display_name="Geo Targeting",
    parameters=[
        ToolParameter(
            name="action",
            type="string",
            description="The operation: 'discover', 'add', or 'delete'.",
            required=True,
            enum=["discover", "add", "delete"],
        ),
        ToolParameter(
            name="location_name",
            type="string",
            description="Target location, city, state, or country name. Used by 'discover'.",
            required=False,
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
        # NOTE: platform-handle params (google_id / meta_key / meta_type /
        # place_id) are deliberately NOT exposed to the LLM. They belong to the
        # search-widget wire format, which calls the geo service directly
        # (agents/location/craft.py) and bypasses this schema. Exposing them here
        # would let the model invent platform IDs with no traceability check —
        # accounts are fetch-traceable, geo keys would not be.
    ],
    execute=_manage_targeting_locations,
)

LOCATION_TOOLS = [confirm_location, manage_targeting_locations]
