"""Platform mapping - deterministic area → platform-handle resolver.

Maps resolved locations to platform-specific handles (Google Ads geo-target
constants or Meta Ads adgeolocation keys). A shared utility, NOT an LLM tool:
called by both location-agent tools after discovery, and by the agent's
``add_location``/``delete_location`` methods after the area list changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, get_args

from app.agents.adzump.platform import is_google, is_meta
from app.agents.adzump.adapters.google.client import google_ads_client
from app.agents.adzump.adapters.google.maps import google_maps_client
from app.agents.adzump.adapters.meta.client import meta_client
from app.agents.adzump.agents.location.models import (
    GoogleGeoLocation,
    MetaGeoLocation,
    Scale,
    TargetArea,
)
from app.agents.adzump._shared import build_ds_headers

logger = logging.getLogger(__name__)

# Every broad scale is exempt from pincode backfill: stamping a pincode on a
# city/state/region/country target would shrink its map polygon to one postal
# code. Derived from the Scale vocabulary so a newly accepted scale can never
# be missed here (PR #91 B6: a hand-written set lacked "region").
BROAD_SCALES = set(get_args(Scale))


class PlatformGeoMapper:
    """Orchestrates location mapping for Google Ads and Meta Ads platforms."""

    def __init__(self, tool_context: dict[str, Any]) -> None:
        self.client_code = tool_context.get("client_code", "")
        self.auth_headers = build_ds_headers(tool_context)

    async def map_target_areas(
        self,
        target_areas: list[dict[str, Any]],
        platform_name: str,
        country_code: str = "IN",
    ) -> list[dict[str, Any]]:
        """Map generic targets to platform-specific geolocation fields.

        Areas are independent, so they map concurrently - a re-map of 25
        discovered neighbourhoods is one round-trip deep, not 50-75."""
        if not target_areas:
            return []
        return list(await asyncio.gather(*(
            self._map_one(dict(area), platform_name, country_code)
            for area in target_areas
        )))

    async def _map_one(
        self, area: dict[str, Any], platform_name: str, country_code: str
    ) -> dict[str, Any]:
        """Enrich one area (platform-agnostic), then resolve its platform handle."""
        # Enrichment first: Meta's pincode-first query and the backfill's
        # scale exemption both want the full 'where' before the lookup.
        await self._geocode_if_missing(area)
        await self._backfill_pincode(area)

        if is_google(platform_name):
            return await self._map_google(area, country_code)
        if is_meta(platform_name):
            return await self._map_meta(area, country_code)
        return area

    async def _map_google(
        self, area: dict[str, Any], country_code: str = "IN"
    ) -> dict[str, Any]:
        """Resolve generic target area to a Google Ads geo target constant."""
        pincode = area.get("pincode")
        city = area.get("city")

        # Only a prior mapping round-trip (nested handle) is trusted - it came
        # from the suggest lookup below. Flat handles are NOT read: they'd be
        # LLM-writable and would skip the lookup that validates them.
        existing = area.get("google") or {}
        resource_name = existing.get("resourceName")
        google_name = existing.get("name")

        # Query suggest_geo_targets without needing account/parent_account IDs.
        # Fall back to area name so manually-added areas (no pincode/city parsed)
        # still resolve to a geo target constant and coordinates.
        lookup_query = pincode or city or area.get("name") or ""
        if lookup_query and not resource_name:
            try:
                results = await google_ads_client.suggest_geo_targets(
                    query=lookup_query,
                    country_code=country_code,
                    client_code=self.client_code,
                    auth_headers=self.auth_headers,
                )
                suggestions = results.get("geoTargetConstantSuggestions") or []
                if suggestions:
                    constant = suggestions[0].get("geoTargetConstant") or {}
                    resource_name = constant.get("resourceName") or constant.get("id")
                    google_name = constant.get("canonicalName") or constant.get("name")
            except Exception as e:
                logger.warning("Google Ads API geo target suggest lookup failed: %s", e)

        # No constant resolved → keyless area falls back to lat/lng proximity
        # (no google handle attached). The model normalizes to the resource name.
        google = (
            GoogleGeoLocation(resourceName=str(resource_name), name=google_name or area.get("name"))
            if resource_name
            else None
        )
        return self._target_area(area, google=google)

    async def _map_meta(
        self, area: dict[str, Any], country_code: str = "IN"
    ) -> dict[str, Any]:
        """Resolve generic target area to a Meta Ads geolocation (zip/city/region/country)."""
        pincode = area.get("pincode")
        city = area.get("city")

        # Only a prior mapping round-trip (nested handle) is trusted - it came
        # from the /search lookup below. Flat handles are NOT read: they'd be
        # LLM-writable and would skip the lookup that validates them.
        existing = area.get("meta") or {}
        meta_key = existing.get("key")
        meta_type = existing.get("type")
        meta_name = existing.get("name")

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
                search_params = {
                    "type": "adgeolocation",
                    "q": query,
                    "location_types": json.dumps([loc_type]),
                    "country_code": country_code,
                }
                response = await meta_client.get(
                    "/search", self.client_code, self.auth_headers, params=search_params
                )
                matches = response.get("data") or []
                if matches:
                    meta_key = matches[0].get("key")
                    # Prefer Meta's canonical type for the matched result.
                    meta_type = matches[0].get("type") or loc_type
                    meta_name = matches[0].get("name")
            except Exception as e:
                logger.warning("Meta Geolocation search lookup failed: %s", e)

        # No key resolved → no handle (Meta rejects keyless entries); mirrors Google.
        meta = (
            MetaGeoLocation(
                type=meta_type or loc_type,
                key=str(meta_key),
                name=meta_name or area.get("name"),
            )
            if meta_key
            else None
        )
        return self._target_area(area, meta=meta)

    async def _geocode_if_missing(self, area: dict[str, Any]) -> None:
        """Populate lat/lng (and any missing address fields) when the area has
        no coordinates yet."""
        if area.get("lat") is not None:
            return
        query = area.get("name") or area.get("city") or area.get("pincode") or ""
        if not query:
            return
        try:
            geo = await google_maps_client.geocode(query)
        except Exception as e:
            logger.warning("Geocode failed for area %r: %s", area.get("name"), e)
            return
        if not geo or geo.get("lat") is None:
            return
        area["lat"] = geo["lat"]
        area["lng"] = geo["lng"]
        for field in ("pincode", "city", "state"):
            if not area.get(field) and geo.get(field):
                area[field] = geo[field]

    async def _backfill_pincode(self, area: dict[str, Any]) -> None:
        """Stamp the postal code on a neighbourhood-scale area missing one.

        The craft map's Feature Layer draws reliable polygons only for postal
        codes (bare neighbourhood names often have no polygon), so a
        pincode-less area would render invisibly. Forward geocodes of
        neighbourhoods frequently omit the postal_code component - reverse
        geocoding the coordinates is the dependable source, the same one
        discover_neighborhoods uses."""
        if area.get("pincode") or (area.get("scale") or "").lower() in BROAD_SCALES:
            return
        if area.get("lat") is None or area.get("lng") is None:
            return
        try:
            candidates = await google_maps_client.reverse_geocode(area["lat"], area["lng"])
        except Exception as e:
            logger.warning(
                "Pincode backfill reverse-geocode failed for %r: %s", area.get("name"), e
            )
            return
        for candidate in candidates:
            if "postal_code" not in candidate.get("types", []):
                continue
            for comp in candidate.get("address_components", []):
                if "postal_code" in comp.get("types", []):
                    area["pincode"] = comp.get("long_name", "").strip()
                    return

    def _target_area(
        self,
        area: dict[str, Any],
        *,
        meta: MetaGeoLocation | None = None,
        google: GoogleGeoLocation | None = None,
    ) -> dict[str, Any]:
        """Compose the generic 'where' with the resolved platform handle.

        Picks fields explicitly so the flat platform inputs (key, type,
        resourceName…) and the legacy geo_level are dropped - the output carries
        the scale once and the platform handle once, in nested form."""
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
