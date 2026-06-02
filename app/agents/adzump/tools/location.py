"""Real-estate location confirmation & geo target discovery tools.

Gates location map confirmations on real-estate keywords and exposes a
unified tool to discover prime targeting areas (radial geocoded neighborhoods
or strategic regional/national market hubs) and emit them immediately to the UI.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

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

    from app.agents.adzump.services.geo import get_business_scope

    scope = get_business_scope(product)
    is_re = _is_real_estate(business_type)

    if not (is_re or scope in ("hyperlocal", "local")):
        logger.info(
            "confirm_location skipped: business_type=%r not real-estate and scope=%r not local/hyperlocal",
            business_type,
            scope,
        )
        return ToolResult(
            success=False,
            error=f"confirm_location only applies to real-estate or local/hyperlocal campaigns — business_type is '{business_type}' and scope is '{scope}'. Skip this step.",
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
        summary=f"Map + prompt shown for '{display or 'unknown location'}'. Wait for the user's reply.",
    )


async def _discover_geo_targets(params: dict, context: dict) -> ToolResult:
    session_ctx = context.get("session_context") or {}
    product_data = session_ctx.get("product_data") or {}

    # 1. Resolve coordinates from cached _location_meta
    loc_meta = session_ctx.get("_location_meta") or {}
    lat = loc_meta.get("lat")
    lng = loc_meta.get("lng")

    coordinates = None
    if lat is not None and lng is not None:
        coordinates = {"lat": lat, "lng": lng}
    else:
        # Fallback: check if the user confirmed a location in campaign_spec, and try to geocode it
        spec = session_ctx.get("campaign_spec") or {}
        location_str = spec.get("location")
        if location_str:
            from app.agents.adzump.adapters.google.maps import google_maps_client

            geo_res = await google_maps_client.geocode(location_str)
            if geo_res:
                coordinates = {"lat": geo_res["lat"], "lng": geo_res["lng"]}
                # Update location meta so it is cached
                session_ctx["_location_meta"] = {
                    "address": geo_res["address"] or location_str,
                    "lat": geo_res["lat"],
                    "lng": geo_res["lng"],
                    "displayName": loc_meta.get("displayName") or "",
                }

    # 2. Call the Orchestrator
    from app.agents.adzump.services.geo import discover_geo_targets as run_discovery

    logger.info(
        "discover_geo_targets_tool: running discovery with coordinates=%r", coordinates
    )

    try:
        targets = await run_discovery(coordinates, product_data)
    except Exception as e:
        logger.exception("discover_geo_targets_tool failed: %s", e)
        return ToolResult(success=False, error=f"Target area discovery failed: {e}")

    # 3. Save target areas into session context & database
    product_data["target_areas"] = targets

    from app.agents.adzump.services.business_storage import save_campaign

    await save_campaign(session_ctx, context)

    # 4. Trigger immediate Craft Panel re-rendering
    from app.agents.adzump.tools.competitor import _emit_final_craft
    from app.agents.adzump.services.business_storage import resolve_url

    stream = context.get("event_stream")
    craft_id = session_ctx.get("craft_id")
    business_url = resolve_url(session_ctx)

    if stream and craft_id and business_url:
        competitive = session_ctx.get("competitor_analysis") or {"competitors": []}
        await _emit_final_craft(
            stream,
            craft_id,
            business_url,
            product_data,
            competitive,
            screenshot_url=product_data.get("primary_screenshot_url")
            or product_data.get("screenshot_url"),
            baked_summary=product_data.get("summary", ""),
        )

    summary = f"Discovered {len(targets)} targeting areas successfully."
    return ToolResult(
        success=True,
        data={"target_areas": targets},
        summary=summary,
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
)


discover_geo_targets = ToolDefinition(
    name="discover_geo_targets",
    description=(
        "Analyze campaign parameters and coordinates to discover prime geographic target areas. "
        "For hyperlocal/local scale, maps neighborhood-level radial target regions (e.g. Richmond Town). "
        "For regional/national/global scale, recommends strategic metropolitan/city hubs with clear justifications. "
        "Returns the unified target list and instantly saves/renders in the UI details card."
    ),
    display_name="Discover Target Areas",
    parameters=[],
    execute=_discover_geo_targets,
)


async def _map_target_areas(params: dict, context: dict) -> ToolResult:
    session_ctx = context.get("session_context") or {}
    product_data = session_ctx.get("product_data") or {}
    spec = session_ctx.get("campaign_spec") or {}
    platform_val = spec.get("platform")

    if not platform_val:
        return ToolResult(
            success=False,
            error="No target platform is configured in the campaign. Set a platform first before mapping.",
        )

    target_areas = product_data.get("target_areas") or []
    if not target_areas:
        return ToolResult(
            success=False,
            error="No targeting areas found to map. Run target area discovery first.",
        )

    from app.agents.adzump.services.geo.mapping import PlatformGeoMapper

    try:
        mapper = PlatformGeoMapper(session_ctx, context)
        mapped = await mapper.map_target_areas(target_areas, platform_val)
        product_data["target_areas"] = mapped
    except Exception as e:
        logger.exception("map_target_areas tool failed: %s", e)
        return ToolResult(success=False, error=f"Target mapping failed: {e}")

    # Save campaign state
    from app.agents.adzump.services.business_storage import save_campaign

    await save_campaign(session_ctx, context)

    # Re-emit Craft-2
    from app.agents.adzump.tools.competitor import _emit_craft2

    stream = context.get("event_stream")
    craft_id = session_ctx.get("craft_id")

    if stream and craft_id:
        try:
            craft_id_2 = f"{craft_id}_craft2"
            await _emit_craft2(stream, craft_id_2, product_data, spec)
        except Exception as ex:
            logger.exception(
                "Failed to emit Campaign Assets Craft-2 during map tool: %s", ex
            )

    return ToolResult(
        success=True,
        data={"target_areas": mapped},
        summary=f"Successfully mapped {len(mapped)} targeting areas to the {platform_val} platform.",
    )


map_target_areas = ToolDefinition(
    name="map_target_areas",
    description=(
        "Resolve the discovered targeting areas to official ad network geolocations (Google Ads Criteria IDs or Meta ZIP keys) based on the selected platform. "
        "Instantly saves the mapping results and updates the side panel targeting card."
    ),
    display_name="Map Target Areas",
    parameters=[],
    execute=_map_target_areas,
)


LOCATION_TOOLS = [confirm_location, discover_geo_targets, map_target_areas]
