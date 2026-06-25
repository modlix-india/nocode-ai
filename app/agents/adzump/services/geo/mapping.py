"""Geo-targeting ad platform mapping service.

Maps resolved locations to platform-specific keys (Google Ads Criteria IDs
or Proximity coordinates, Meta Ads ZIP codes or location keys).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agents.adzump.platform import is_google, is_meta
from app.agents.adzump.adapters.google.client import google_ads_client
from app.agents.adzump.adapters.google.maps import google_maps_client
from app.agents.adzump.adapters.meta.client import meta_client
from app.agents.adzump._shared import build_ds_headers

logger = logging.getLogger(__name__)


class PlatformGeoMapper:
    """Orchestrates location mapping for Google Ads and Meta Ads platforms."""

    def __init__(
        self, session_context: dict[str, Any], tool_context: dict[str, Any]
    ) -> None:
        self.session_ctx = session_context
        self.client_code = tool_context.get("client_code", "")
        self.auth_headers = build_ds_headers(tool_context)

    async def _geocode_if_missing(self, area: dict[str, Any]) -> None:
        """Populate lat/lng from geocoding when the area has no coordinates yet."""
        if area.get("lat") is not None:
            return
        query = area.get("name") or area.get("city") or area.get("pincode") or ""
        if not query:
            return
        try:
            geo = await google_maps_client.geocode(query)
            if geo and geo.get("lat") is not None:
                area["lat"] = geo["lat"]
                area["lng"] = geo["lng"]
        except Exception as e:
            logger.warning("Geocode failed for area %r: %s", area.get("name"), e)

    async def map_target_areas(
        self,
        target_areas: list[dict[str, Any]],
        platform_name: str,
        country_code: str = "IN",
    ) -> list[dict[str, Any]]:
        """Map generic targets to platform-specific geolocation fields."""
        if not target_areas:
            return []

        mapped_areas = []

        for area in target_areas:
            # Create a clean copy to avoid in-place side effects
            area_copy = dict(area)

            if is_google(platform_name):
                mapped_area = await self._map_google(area_copy, country_code)
            elif is_meta(platform_name):
                mapped_area = await self._map_meta(area_copy, country_code)
            else:
                mapped_area = area_copy

            mapped_areas.append(mapped_area)

        return mapped_areas

    async def _map_google(
        self, area: dict[str, Any], country_code: str = "IN"
    ) -> dict[str, Any]:
        """Resolve generic target area to Google Ads Criteria ID or Proximity structure."""
        pincode = area.get("pincode")
        city = area.get("city")

        # Initialize from existing values if present
        google_id = area.get("google_id")
        google_name = area.get("google_name")

        # Query suggest_geo_targets without needing account/parent_account IDs.
        # Fall back to area name so manually-added areas (no pincode/city parsed)
        # still get a google_id and coordinates.
        lookup_query = pincode or city or area.get("name") or ""
        if lookup_query and not google_id:
            try:
                results = await google_ads_client.suggest_geo_targets(
                    query=lookup_query,
                    country_code=country_code,
                    client_code=self.client_code,
                    auth_headers=self.auth_headers,
                )
                suggestions = results.get("geoTargetConstantSuggestions") or []
                if suggestions:
                    const = suggestions[0].get("geoTargetConstant") or {}
                    google_id = const.get("resourceName") or const.get("id")
                    google_name = const.get("canonicalName") or const.get("name")
            except Exception as e:
                logger.warning("Google Ads API geo target suggest lookup failed: %s", e)

        # Apply mapped values or fallback to proximity targets for local scans
        if google_id:
            str_id = str(google_id)
            if not str_id.startswith("geoTargetConstants/"):
                str_id = f"geoTargetConstants/{str_id}"
            area["google_id"] = str_id
            area["google_name"] = google_name or area.get("name")

        await self._geocode_if_missing(area)
        area.pop("google_proximity", None)
        return area

    async def _map_meta(
        self, area: dict[str, Any], country_code: str = "IN"
    ) -> dict[str, Any]:
        """Resolve generic target area to Meta Ads ZIP key, City key, or Radial object."""
        pincode = area.get("pincode")
        city = area.get("city")

        meta_key = None
        meta_type = None
        meta_name = None

        # Fall back to area name so manually-added areas (no pincode/city) still resolve.
        query, loc_type = (
            (pincode, "zip") if pincode
            else (city, "city") if city
            else (area.get("name") or "", "city")
        )
        try:
            if query:
                params = {
                    "type": "adgeolocation",
                    "q": query,
                    "location_types": json.dumps([loc_type]),
                    "country_code": country_code,
                }
                res = await meta_client.get(
                    "/search", self.client_code, self.auth_headers, params=params
                )
                data = res.get("data") or []
                if data:
                    meta_key = data[0].get("key")
                    meta_type = loc_type
                    meta_name = data[0].get("name")

        except Exception as e:
            logger.warning("Meta Geolocation search lookup failed: %s", e)

        # Apply mapped values or fallback to radial structures
        if meta_key:
            area["meta_key"] = str(meta_key)
            area["meta_type"] = meta_type
            area["meta_name"] = meta_name or area.get("name")

        await self._geocode_if_missing(area)
        area.pop("meta_radial", None)
        return area
