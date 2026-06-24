"""Location confirmation and geo-targeting tools for Adzump.

Provides tools for map validation of business locations, platform-specific
geo-targeting discovery, and manual targeting edits (additions/deletions).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.config import settings
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.adapters.google.maps import google_maps_client
from app.agents.adzump.services.geo.discovery import (
    is_local_business,
    discover_geo_targets as run_discover_geo_targets,
)
from app.agents.adzump.services.geo.mapping import PlatformGeoMapper
from app.agents.adzump.services.business_storage import save_campaign, resolve_url

logger = logging.getLogger(__name__)


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
    business_scale = (product.get("business_scale") or "").strip()

    # Relax constraints: allow any local physical business to validate location on map
    if not is_local_business(business_scale):
        logger.info(
            "confirm_location skipped: business_scale=%r is not local", business_scale
        )
        return ToolResult(
            success=False,
            error=(
                f"confirm_location only applies to local physical businesses. "
                f"Business scale is '{business_scale}'. Skip this step."
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
    session_ctx = context.get("session_context") or {}
    product = session_ctx.get("product_data") or {}
    spec = session_ctx.get("campaign_spec") or {}

    location_name = params.get("location_name")
    platform = spec.get("platform") or params.get("platform") or "Google Ads"

    loc_meta = session_ctx.get("_location_meta") or {}
    coordinates = None
    if loc_meta.get("lat") is not None and loc_meta.get("lng") is not None:
        coordinates = {"lat": float(loc_meta["lat"]), "lng": float(loc_meta["lng"])}

    if not location_name:
        location_name = (
            loc_meta.get("address")
            or spec.get("location")
            or _detected_location(product)
        )

    if not coordinates and location_name:
        try:
            geo = await google_maps_client.geocode(location_name)
            if geo and geo.get("lat") is not None and geo.get("lng") is not None:
                coordinates = {"lat": float(geo["lat"]), "lng": float(geo["lng"])}
                loc_meta["lat"] = geo["lat"]
                loc_meta["lng"] = geo["lng"]
                if geo.get("country_code"):
                    loc_meta["country_code"] = geo["country_code"]
                if geo.get("address") and not loc_meta.get("address"):
                    loc_meta["address"] = geo["address"]
                if "place_id" in geo:
                    loc_meta["place_id"] = geo["place_id"]
                session_ctx["_location_meta"] = loc_meta
        except Exception as ge:
            logger.warning("Geocoding location_name '%s' failed: %s", location_name, ge)

    if coordinates:
        product["product_coordinates"] = coordinates

    stream = context.get("event_stream")
    tool_use_id = f"discover_geo_targets_{uuid.uuid4().hex[:8]}"

    if stream:
        await stream.emit_tool_start(
            tool_name="discover_geo_targets",
            tool_input={"location_name": location_name, "platform": platform},
            tool_use_id=tool_use_id,
            display_name="Discover Geo Targets",
        )

    try:
        if stream:
            await stream.emit_tool_update(
                tool_use_id=tool_use_id,
                message=f"Fetching target areas for coordinates {coordinates}...",
            )
        resolved_locations = await run_discover_geo_targets(
            coordinates, product, country_code=loc_meta.get("country_code", "IN")
        )
        logger.info(
            "discover_geo_targets: resolved %d locations for platform=%s",
            len(resolved_locations),
            platform,
        )

        is_google = "google" in platform.lower()
        mapping_key = "google_mapped_locations" if is_google else "meta_mapped_locations"

        mapper = PlatformGeoMapper(session_ctx, context)
        if stream:
            await stream.emit_tool_update(
                tool_use_id=tool_use_id,
                message=f"Mapping {len(resolved_locations)} target locations to {platform}...",
            )
        mapped_locations = await mapper.map_target_areas(
            resolved_locations, platform, loc_meta.get("country_code", "IN")
        )

        loc_meta[mapping_key] = mapped_locations
        session_ctx["_location_meta"] = loc_meta
        product["target_areas"] = mapped_locations
        product[mapping_key] = mapped_locations

        if stream and mapped_locations:
            await stream.emit_data(
                "suggested_locations",
                {
                    "locations": [loc["name"] for loc in mapped_locations],
                    "targeting_type": product.get("business_scale", "national"),
                    "location": location_name,
                },
            )

        await save_campaign(session_ctx, context)

        try:
            from app.agents.adzump.tools.competitor import _emit_final_craft

            url = resolve_url(session_ctx)
            craft_id = session_ctx.get("craft_id") or session_ctx.get("_craft_id")
            if stream and craft_id and url:
                competitive = session_ctx.get("competitor_analysis") or {"competitors": []}
                await _emit_final_craft(
                    stream,
                    craft_id,
                    url,
                    product,
                    competitive,
                    screenshot_url=(product.get("primary_screenshot_url") or product.get("screenshot_url")),
                    baked_summary=(product.get("summary") or (session_ctx.get("product_profile") or {}).get("summary", "")),
                    platform=platform,
                )
        except Exception as ce:
            logger.warning("Craft panel re-render after geo-target discovery failed: %s", ce)

        if stream:
            await stream.emit_tool_result(
                tool_name="discover_geo_targets",
                success=True,
                summary=f"Discovered and mapped {len(mapped_locations)} targeting locations for {platform}.",
                tool_use_id=tool_use_id,
            )

        return ToolResult(
            success=True,
            data={"target_areas": mapped_locations},
            summary=f"Resolved {len(mapped_locations)} targeting locations for {platform}.",
        )

    except Exception as e:
        if stream:
            await stream.emit_tool_result(
                tool_name="discover_geo_targets",
                success=False,
                summary=f"Failed to resolve geo-targeting: {e}",
                tool_use_id=tool_use_id,
            )
        logger.exception("discover_geo_targets failed: %s", e)
        return ToolResult(
            success=False,
            error=f"Failed to resolve geo-targeting: {e}",
        )


async def _modify_targeting_location(params: dict, context: dict) -> ToolResult:
    session_ctx = context.get("session_context")
    if session_ctx is None:
        return ToolResult(success=False, error="No session context available.")

    product_data = session_ctx.setdefault("product_data", {})
    spec = session_ctx.setdefault("campaign_spec", {})
    platform = (spec.get("platform") or "Google Ads").lower().strip()

    action = params.get("action")
    target_areas = product_data.setdefault("target_areas", [])

    if action not in ("add", "delete"):
        return ToolResult(success=False, error=f"Invalid action: {action}")

    if action == "add":
        name = params.get("name")
        if not name:
            return ToolResult(success=False, error="Name is required for 'add' action.")

        area = {
            "name": name,
            "city": params.get("city") or "",
            "state": params.get("state") or "",
            "pincode": params.get("pincode") or "",
            "lat": params.get("lat"),
            "lng": params.get("lng"),
            "distance_km": params.get("radius") or 5.0,
        }
        if params.get("place_id"):
            area["place_id"] = params.get("place_id")
        if params.get("google_id"):
            area["google_id"] = params.get("google_id")
            area["google_name"] = name
        if params.get("meta_key"):
            area["meta_key"] = params.get("meta_key")
            area["meta_name"] = name

        target_areas.append(area)

    elif action == "delete":
        index = params.get("index")  # 1-based index
        if index is None or index < 1 or index > len(target_areas):
            return ToolResult(
                success=False,
                error=f"Invalid index {index}. There are only {len(target_areas)} target areas.",
            )
        target_areas.pop(index - 1)

    # Re-map target areas
    from app.agents.adzump.services.geo.mapping import PlatformGeoMapper

    loc_meta = session_ctx.get("_location_meta") or {}
    cc = loc_meta.get("country_code") or "IN"

    try:
        mapper = PlatformGeoMapper(session_ctx, context)
        product_data["target_areas"] = await mapper.map_target_areas(
            target_areas, platform, cc
        )
    except Exception as e:
        logger.warning(
            "PlatformGeoMapper mapping failed in modify_targeting_location tool: %s", e
        )

    # Sync mapped targets to platform-specific key list
    is_google = "google" in platform.lower()
    mapping_key = "google_mapped_locations" if is_google else "meta_mapped_locations"
    loc_meta[mapping_key] = product_data["target_areas"]
    session_ctx["_location_meta"] = loc_meta
    product_data[mapping_key] = product_data["target_areas"]

    # Save campaign state
    from app.agents.adzump.services.business_storage import save_campaign

    await save_campaign(session_ctx, context)

    # Re-emit final Craft to update the map UI
    from app.agents.adzump.tools.competitor import _emit_final_craft
    from app.agents.adzump.services.business_storage import resolve_url

    stream = context.get("event_stream")
    craft_id = session_ctx.get("craft_id") or session_ctx.get("_craft_id")
    url = resolve_url(session_ctx)

    if stream and craft_id and url:
        try:
            competitive = session_ctx.get("competitor_analysis") or {"competitors": []}
            screenshot_url = product_data.get(
                "primary_screenshot_url"
            ) or product_data.get("screenshot_url")
            baked_summary = product_data.get("summary") or (
                session_ctx.get("product_profile") or {}
            ).get("summary", "")
            spec = session_ctx.get("campaign_spec") or {}
            caller_platform = spec.get("platform", "Google Ads")
            await _emit_final_craft(
                stream,
                craft_id,
                url,
                product_data,
                competitive,
                screenshot_url=screenshot_url,
                baked_summary=baked_summary,
                platform=caller_platform,
            )
        except Exception as ex:
            logger.exception(
                "Failed to emit Campaign Assets Craft during modify tool: %s", ex
            )

    action_past = "added" if action == "add" else "deleted"
    return ToolResult(
        success=True,
        data={"target_areas": product_data["target_areas"]},
        summary=f"Successfully {action_past} targeting area.",
    )


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
