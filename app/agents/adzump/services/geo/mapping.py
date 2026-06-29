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
from app.agents.adzump.services.geo.models import (
    GoogleGeoLocation,
    MetaGeoLocation,
    TargetArea,
)
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

    @staticmethod
    def _target_area(
        area: dict[str, Any],
        *,
        meta: MetaGeoLocation | None = None,
        google: GoogleGeoLocation | None = None,
    ) -> dict[str, Any]:
        """Compose the generic 'where' with the resolved platform handle.

        Picks fields explicitly so the flat platform inputs (meta_key, google_id…)
        and the legacy geo_level are dropped — the output carries the scale once
        and the platform handle once, in nested form."""
        return TargetArea(
            name=area.get("name") or "",
            city=area.get("city") or "",
            state=area.get("state") or "",
            pincode=area.get("pincode") or "",
            lat=area.get("lat"),
            lng=area.get("lng"),
            distance_km=area.get("distance_km") or 0.0,
            place_id=area.get("place_id"),
            scale=(area.get("scale") or "").strip().lower() or None,
            meta=meta,
            google=google,
        ).model_dump(exclude_none=True)

    async def _map_google(
        self, area: dict[str, Any], country_code: str = "IN"
    ) -> dict[str, Any]:
        """Resolve generic target area to a Google Ads geo target constant."""
        pincode = area.get("pincode")
        city = area.get("city")

        # Existing resolution may arrive nested (a prior mapping round-trip) or
        # flat (the search widget / manual add). Read tolerantly.
        existing = area.get("google") or {}
        google_id = existing.get("id") or area.get("google_id")
        google_name = existing.get("name") or area.get("google_name")

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

        await self._geocode_if_missing(area)
        area.pop("google_proximity", None)

        # No constant resolved → keyless area falls back to lat/lng proximity
        # (no google handle attached). The model normalizes id to its resource name.
        google = (
            GoogleGeoLocation(id=str(google_id), name=google_name or area.get("name"))
            if google_id
            else None
        )
        return self._target_area(area, google=google)

    async def _map_meta(
        self, area: dict[str, Any], country_code: str = "IN"
    ) -> dict[str, Any]:
        """Resolve generic target area to a Meta Ads geolocation (zip/city/region/country)."""
        pincode = area.get("pincode")
        city = area.get("city")

        # Existing resolution may arrive nested (a prior mapping round-trip) or
        # flat (the search widget / manual add). Read tolerantly so a failed
        # re-lookup never drops what was already resolved.
        existing = area.get("meta") or {}
        meta_key = existing.get("key") or area.get("meta_key")
        meta_type = existing.get("type") or area.get("meta_type")
        meta_name = existing.get("name") or area.get("meta_name")

        # Broad campaigns tag the scale the strategist picked; map it to Meta's
        # location_types so a country/state is searched as such, not as a city.
        # Pincode still wins (it's the most specific signal); otherwise fall back
        # to area name so manually-added areas (no pincode/city) still resolve.
        meta_level = {"country": "country", "state": "region", "region": "region"}.get(
            (area.get("scale") or "").strip().lower()
        )
        if pincode:
            query, loc_type = pincode, "zip"
        elif meta_level:
            query, loc_type = (area.get("name") or city or ""), meta_level
        elif city:
            query, loc_type = city, "city"
        else:
            query, loc_type = (area.get("name") or ""), "city"
        if query and not meta_key:
            try:
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
                    # Prefer Meta's canonical type for the matched result.
                    meta_type = data[0].get("type") or loc_type
                    meta_name = data[0].get("name")
            except Exception as e:
                logger.warning("Meta Geolocation search lookup failed: %s", e)

        await self._geocode_if_missing(area)
        area.pop("meta_radial", None)

        # type is always set (required by the model): Meta adset creation buckets
        # each target by type, so it must be present even when the key lookup
        # found nothing (the lat/lng radial fallback still needs the type).
        meta = MetaGeoLocation(
            type=meta_type or loc_type,
            key=str(meta_key) if meta_key else None,
            name=(meta_name or area.get("name")) if meta_key else None,
        )
        return self._target_area(area, meta=meta)
