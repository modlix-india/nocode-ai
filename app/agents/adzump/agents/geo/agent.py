"""GeoTargetingAgent — orchestrator for geo-target discovery and modification.

Owns the full geocode → discover → platform-map → persist → craft-re-emit
pipeline. Exposed as a singleton so location.py tools delegate to it rather
than embedding the orchestration inline.
"""

from __future__ import annotations

import logging

from app.core.tools.base import ToolResult
from app.agents.adzump.adapters.google.maps import google_maps_client
from app.agents.adzump.platform import is_google
from app.agents.adzump.services.geo.discovery import (
    discover_geo_targets as run_discover_geo_targets,
)
from app.agents.adzump.services.geo.mapping import PlatformGeoMapper
from app.agents.adzump.services.business_storage import save_campaign, resolve_url
from app.agents.adzump.tools.craft import emit_craft_panel as _emit_final_craft

logger = logging.getLogger(__name__)


def _location_str(product: dict) -> str:
    """Extract a plain string location from product_data.location (str or dict)."""
    loc = product.get("location") or {}
    if isinstance(loc, str):
        return loc.strip()
    if isinstance(loc, dict):
        return (loc.get("location") or "").strip()
    return ""


class GeoTargetingAgent:
    """Singleton that owns the geo-targeting orchestration shared by location tools."""

    _instance: "GeoTargetingAgent | None" = None

    @classmethod
    def get_instance(cls) -> "GeoTargetingAgent":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def _persist_and_rerender(
        self,
        session_ctx: dict,
        context: dict,
        product: dict,
        platform: str,
    ) -> None:
        """Save campaign and re-emit the craft panel. Swallows failures gracefully."""
        await save_campaign(session_ctx, context)
        stream = context.get("event_stream")
        craft_id = session_ctx.get("craft_id") or session_ctx.get("_craft_id")
        url = resolve_url(session_ctx)
        if stream and craft_id and url:
            try:
                competitive = session_ctx.get("competitor_analysis") or {"competitors": []}
                await _emit_final_craft(
                    stream,
                    craft_id,
                    url,
                    product,
                    competitive,
                    screenshot_url=(
                        product.get("primary_screenshot_url") or product.get("screenshot_url")
                    ),
                    baked_summary=(
                        (session_ctx.get("product_profile") or {}).get("summary")
                        or product.get("summary", "")
                    ),
                    platform=platform,
                )
            except Exception as e:
                logger.warning("Craft panel re-render failed: %s", e)

    async def discover(self, params: dict, context: dict) -> ToolResult:
        """Geocode the session location, run discovery, map to platform, persist."""
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
                or _location_str(product)
            )

        if not coordinates and location_name:
            try:
                geo = await google_maps_client.geocode(location_name)
                if geo and geo.get("lat") is not None and geo.get("lng") is not None:
                    coordinates = {"lat": float(geo["lat"]), "lng": float(geo["lng"])}
                    loc_meta.update({
                        "lat": geo["lat"],
                        "lng": geo["lng"],
                        **({} if not geo.get("country_code") else {"country_code": geo["country_code"]}),
                        **({} if loc_meta.get("address") or not geo.get("address") else {"address": geo["address"]}),
                        **({} if "place_id" not in geo else {"place_id": geo["place_id"]}),
                    })
                    session_ctx["_location_meta"] = loc_meta
            except Exception as ge:
                logger.warning("Geocoding '%s' failed: %s", location_name, ge)

        if coordinates:
            product["product_coordinates"] = coordinates

        stream = context.get("event_stream")
        tool_use_id = context.get("tool_use_id", "")

        try:
            if stream:
                await stream.emit_tool_update(
                    tool_use_id=tool_use_id,
                    message=f"Fetching target areas for {coordinates}...",
                )

            resolved = await run_discover_geo_targets(
                coordinates, product, country_code=loc_meta.get("country_code", "IN")
            )
            logger.info(
                "discover_geo_targets: resolved %d locations for platform=%s",
                len(resolved), platform,
            )

            mapping_key = "google_mapped_locations" if is_google(platform) else "meta_mapped_locations"

            mapper = PlatformGeoMapper(session_ctx, context)
            if stream:
                await stream.emit_tool_update(
                    tool_use_id=tool_use_id,
                    message=f"Mapping {len(resolved)} locations to {platform}...",
                )
            mapped = await mapper.map_target_areas(
                resolved, platform, loc_meta.get("country_code", "IN")
            )

            loc_meta[mapping_key] = mapped
            session_ctx["_location_meta"] = loc_meta
            product["target_areas"] = mapped
            product[mapping_key] = mapped

            if stream and mapped:
                await stream.emit_data(
                    "suggested_locations",
                    {
                        "locations": [loc["name"] for loc in mapped],
                        "targeting_type": product.get("business_scale", "national"),
                        "location": location_name,
                    },
                )

            await self._persist_and_rerender(session_ctx, context, product, platform)

            return ToolResult(
                success=True,
                data={"target_areas": mapped},
                summary=f"Discovered and mapped {len(mapped)} targeting locations for {platform}.",
            )

        except Exception as e:
            logger.exception("discover_geo_targets failed: %s", e)
            return ToolResult(success=False, error=f"Failed to resolve geo-targeting: {e}")

    async def modify(self, params: dict, context: dict) -> ToolResult:
        """Add or delete a targeting area and re-sync platform keys + craft panel."""
        session_ctx = context.get("session_context")
        if session_ctx is None:
            return ToolResult(success=False, error="No session context available.")

        product = session_ctx.setdefault("product_data", {})
        spec = session_ctx.setdefault("campaign_spec", {})
        platform = (spec.get("platform") or "Google Ads").strip()
        target_areas = product.setdefault("target_areas", [])

        action = params.get("action")
        if action not in ("add", "delete"):
            return ToolResult(success=False, error=f"Invalid action: {action!r}")

        if action == "add":
            name = params.get("name")
            if not name:
                return ToolResult(success=False, error="Name is required for 'add' action.")
            area: dict = {
                "name": name,
                "city": params.get("city") or "",
                "state": params.get("state") or "",
                "pincode": params.get("pincode") or "",
                "lat": params.get("lat"),
                "lng": params.get("lng"),
                "distance_km": params.get("radius") or 5.0,
            }
            if params.get("place_id"):
                area["place_id"] = params["place_id"]
            if params.get("google_id"):
                area["google_id"] = params["google_id"]
                area["google_name"] = name
            if params.get("meta_key"):
                area["meta_key"] = params["meta_key"]
                area["meta_name"] = name
            target_areas.append(area)

        else:  # delete
            index = params.get("index")
            if index is None or index < 1 or index > len(target_areas):
                return ToolResult(
                    success=False,
                    error=f"Invalid index {index}. There are only {len(target_areas)} target areas.",
                )
            target_areas.pop(index - 1)

        loc_meta = session_ctx.get("_location_meta") or {}
        cc = loc_meta.get("country_code") or "IN"
        try:
            mapper = PlatformGeoMapper(session_ctx, context)
            product["target_areas"] = await mapper.map_target_areas(target_areas, platform, cc)
        except Exception as e:
            logger.warning("PlatformGeoMapper failed in modify: %s", e)

        mapping_key = "google_mapped_locations" if is_google(platform) else "meta_mapped_locations"
        loc_meta[mapping_key] = product["target_areas"]
        session_ctx["_location_meta"] = loc_meta
        product[mapping_key] = product["target_areas"]

        caller_platform = spec.get("platform") or "Google Ads"

        logger.info(
            "modify_targeting_location: action=%s platform=%s areas=%d "
            "mapping_key=%s stream=%s craft_id=%s url=%s first_area=%s",
            action, caller_platform, len(product["target_areas"]),
            mapping_key,
            bool(context.get("event_stream")),
            session_ctx.get("craft_id") or session_ctx.get("_craft_id"),
            (session_ctx.get("product_profile") or {}).get("url", ""),
            product["target_areas"][-1] if product["target_areas"] else None,
        )

        await self._persist_and_rerender(session_ctx, context, product, caller_platform)

        # Re-emit the location chips so the search widget reflects the new list.
        stream = context.get("event_stream")
        if stream and product["target_areas"]:
            try:
                loc_name = (
                    loc_meta.get("address")
                    or spec.get("location")
                    or ""
                )
                await stream.emit_data("suggested_locations", {
                    "locations": [a["name"] for a in product["target_areas"] if a.get("name")],
                    "targeting_type": product.get("business_scale", "national"),
                    "location": loc_name,
                })
            except Exception as e:
                logger.debug("suggested_locations re-emit after modify failed: %s", e)

        action_past = "added" if action == "add" else "deleted"
        return ToolResult(
            success=True,
            data={"target_areas": product["target_areas"]},
            summary=f"Successfully {action_past} targeting area.",
        )


def get_geo_targeting_agent() -> GeoTargetingAgent:
    """Module-level accessor for the shared GeoTargetingAgent singleton."""
    return GeoTargetingAgent.get_instance()
