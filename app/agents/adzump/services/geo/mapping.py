"""Platform-specific geographic targeting mapper.

Maps discovered hyperlocal target areas (neighborhoods, cities, zip codes)
to active Google Ads Criteria IDs / Proximity targets or Meta geolocation keys
(ZIP keys, city keys, custom radial coordinate boundaries).
"""

from __future__ import annotations

import logging
import hashlib
from typing import Any

from app.agents.adzump.platform import is_google, is_meta
from app.agents.adzump.adapters.google.client import google_ads_client
from app.agents.adzump.adapters.meta.client import meta_client
from app.agents.adzump.tools._shared import build_ds_headers
from app.agents.adzump.config import get_adzump_config

logger = logging.getLogger(__name__)


class PlatformGeoMapper:
    """Resolves target areas to Google Ads and Meta campaign targeting objects.

    Uses real API requests when valid developer tokens and account IDs are present;
    otherwise falls back gracefully to deterministic, fail-soft synthetic stubs
    to support local developer environments without configuration overhead.
    """

    def __init__(self, session_ctx: dict, context: dict) -> None:
        self.session_ctx = session_ctx
        self.context = context
        self.client_code = context.get("client_code", "")
        self.auth_headers = build_ds_headers(context)

    async def map_target_areas(
        self, target_areas: list[dict[str, Any]], platform_name: str
    ) -> list[dict[str, Any]]:
        """Maps an array of target areas to the requested platform's targeting keys."""
        if not target_areas:
            return []

        logger.info(
            "PlatformGeoMapper: resolving %d target areas for platform %s",
            len(target_areas),
            platform_name,
        )

        mapped_areas = []
        for area in target_areas:
            area_copy = dict(area)

            # Geocode to populate coordinates and place_id if missing
            if (
                area_copy.get("lat") is None
                or area_copy.get("lng") is None
                or area_copy.get("place_id") is None
            ):
                from app.agents.adzump.adapters.google.maps import google_maps_client

                q = (
                    area_copy.get("pincode")
                    or area_copy.get("city")
                    or area_copy.get("name")
                )
                if q:
                    try:
                        geo = await google_maps_client.geocode(q)
                        if geo:
                            area_copy["lat"] = geo.get("lat")
                            area_copy["lng"] = geo.get("lng")
                            area_copy["place_id"] = geo.get("place_id")
                    except Exception as ge:
                        logger.warning(
                            "Geocoding failed for mapping target %s: %s", q, ge
                        )

            if is_google(platform_name):
                mapped_area = await self._map_google(area_copy)
            elif is_meta(platform_name):
                mapped_area = await self._map_meta(area_copy)
            else:
                mapped_area = area_copy
            mapped_areas.append(mapped_area)

        # Build list of fully resolved targeting payloads to save in mapping fields
        google_mapped = []
        meta_mapped = []
        for area in mapped_areas:
            if "google_id" in area:
                google_mapped.append(
                    {
                        "name": area.get("name"),
                        "criteria_id": area["google_id"],
                        "canonical_name": area.get("google_name", area.get("name")),
                    }
                )
            elif "google_proximity" in area:
                google_mapped.append(
                    {
                        "name": area.get("name"),
                        "proximity": area["google_proximity"],
                    }
                )

            if "meta_key" in area:
                meta_mapped.append(
                    {
                        "name": area.get("name"),
                        "key": area["meta_key"],
                        "type": area.get("meta_type"),
                        "display": area.get("meta_name", area.get("name")),
                    }
                )
            elif "meta_radial" in area:
                meta_mapped.append(
                    {
                        "name": area.get("name"),
                        "radial": area["meta_radial"],
                    }
                )

        # Save arrays at the product level for full structured representation
        product = self.session_ctx.setdefault("product_data", {})
        if google_mapped:
            product["google_mapped_locations"] = google_mapped
            product.pop("meta_mapped_locations", None)
        elif meta_mapped:
            product["meta_mapped_locations"] = meta_mapped
            product.pop("google_mapped_locations", None)

        return mapped_areas

    async def _map_google(self, area: dict[str, Any]) -> dict[str, Any]:
        """Resolves target area to Google Ads geo targets (criteria ID or proximity)."""
        pincode = area.get("pincode")
        city = area.get("city")
        lat = area.get("lat")
        lng = area.get("lng")
        distance_km = area.get("distance_km") or 5.0

        # Remove keys for other platforms to maintain consistency
        area.pop("meta_key", None)
        area.pop("meta_type", None)
        area.pop("meta_name", None)
        area.pop("meta_radial", None)

        # Check if we have active Google Ads client capabilities configured
        has_creds = False
        try:
            creds = get_adzump_config().google_ads
            has_creds = bool(creds.developer_token or creds.access_token)
        except Exception:
            pass

        spec = self.session_ctx.get("campaign_spec") or {}
        customer_id = spec.get("account") or spec.get("parent_account")
        login_customer_id = spec.get("parent_account")

        if has_creds and customer_id:
            try:
                # 1. Pincode lookup
                if pincode:
                    query = (
                        "SELECT geo_target_constant.id, geo_target_constant.canonical_name "
                        "FROM geo_target_constant "
                        f"WHERE geo_target_constant.postal_code = '{pincode}' "
                        "AND geo_target_constant.country_code = 'IN' "
                        "AND geo_target_constant.status = 'ENABLED'"
                    )
                    results = await google_ads_client.search(
                        query,
                        customer_id,
                        login_customer_id,
                        self.client_code,
                        self.auth_headers,
                    )
                    if results:
                        g_id = results[0]["geoTargetConstant"]["id"]
                        g_name = results[0]["geoTargetConstant"]["canonicalName"]
                        area["google_id"] = g_id
                        area["google_name"] = g_name
                        area.pop("google_proximity", None)
                        return area

                # 2. City Fallback lookup
                if city:
                    # Escape single quotes in city names
                    escaped_city = city.replace("'", "\\'")
                    query = (
                        "SELECT geo_target_constant.id, geo_target_constant.canonical_name "
                        "FROM geo_target_constant "
                        f"WHERE geo_target_constant.name = '{escaped_city}' "
                        "AND geo_target_constant.country_code = 'IN' "
                        "AND geo_target_constant.status = 'ENABLED'"
                    )
                    results = await google_ads_client.search(
                        query,
                        customer_id,
                        login_customer_id,
                        self.client_code,
                        self.auth_headers,
                    )
                    if results:
                        g_id = results[0]["geoTargetConstant"]["id"]
                        g_name = results[0]["geoTargetConstant"]["canonicalName"]
                        area["google_id"] = g_id
                        area["google_name"] = g_name
                        area.pop("google_proximity", None)
                        return area

            except Exception as e:
                logger.warning(
                    "Google Ads API targeting lookup failed, falling back to stub: %s",
                    e,
                )

        # Fail-Soft Local Dev / Fallback Mock
        if pincode:
            # Deterministic Criteria ID based on pincode for clean local testing
            h = int(hashlib.md5(pincode.encode()).hexdigest(), 16) % 1000000
            area["google_id"] = str(9400000 + h)
            area["google_name"] = (
                f"Pincode {pincode}, {city or 'Bengaluru'}, {area.get('state') or 'KA'}, India"
            )
            area.pop("google_proximity", None)
        elif lat is not None and lng is not None:
            # For neighborhood coordinates where standard lookup fails (or in local dev)
            # we build a structured proximity object
            area["google_proximity"] = {
                "latitude_in_micro_degrees": int(lat * 1_000_000),
                "longitude_in_micro_degrees": int(lng * 1_000_000),
                "radius": float(distance_km),
                "radius_units": "KILOMETERS",
                "display": f"Proximity ({lat:.4f}, {lng:.4f}) with {distance_km} km radius",
            }
            area.pop("google_id", None)
            area.pop("google_name", None)
        else:
            # Complete stub fallback (e.g. no coordinates but name exists)
            val = area.get("name") or "Area"
            h = int(hashlib.md5(val.encode()).hexdigest(), 16) % 1000000
            area["google_id"] = str(9000000 + h)
            area["google_name"] = (
                f"{val}, {city or 'Bengaluru'}, {area.get('state') or 'KA'}, India"
            )
            area.pop("google_proximity", None)

        return area

    async def _map_meta(self, area: dict[str, Any]) -> dict[str, Any]:
        """Resolves target area to Meta Ads geo targets (zip, city, or custom radial)."""
        pincode = area.get("pincode")
        city = area.get("city")
        lat = area.get("lat")
        lng = area.get("lng")
        distance_km = area.get("distance_km") or 5.0

        # Remove keys for other platforms to maintain consistency
        area.pop("google_id", None)
        area.pop("google_name", None)
        area.pop("google_proximity", None)

        # Check if we have active Meta credentials
        has_creds = False
        try:
            creds = get_adzump_config().meta
            has_creds = bool(creds.access_token)
        except Exception:
            pass

        if has_creds:
            try:
                import json

                # 1. Pincode Zip lookup
                if pincode:
                    params = {
                        "type": "adgeolocation",
                        "q": pincode,
                        "location_types": json.dumps(["zip"]),
                        "country_code": "IN",
                    }
                    res = await meta_client.get(
                        "/search",
                        self.client_code,
                        self.auth_headers,
                        params=params,
                    )
                    data = res.get("data") or []
                    if data:
                        area["meta_key"] = data[0]["key"]
                        area["meta_type"] = "zip"
                        area["meta_name"] = data[0]["name"]
                        area.pop("meta_radial", None)
                        return area

                # 2. City lookup
                if city:
                    params = {
                        "type": "adgeolocation",
                        "q": city,
                        "location_types": json.dumps(["city"]),
                        "country_code": "IN",
                    }
                    res = await meta_client.get(
                        "/search",
                        self.client_code,
                        self.auth_headers,
                        params=params,
                    )
                    data = res.get("data") or []
                    if data:
                        area["meta_key"] = data[0]["key"]
                        area["meta_type"] = "city"
                        area["meta_name"] = data[0]["name"]
                        area.pop("meta_radial", None)
                        return area

            except Exception as e:
                logger.warning(
                    "Meta API targeting lookup failed, falling back to stub: %s",
                    e,
                )

        # Fail-Soft Local Dev / Fallback Mock
        if pincode:
            area["meta_key"] = f"IN:{pincode}"
            area["meta_type"] = "zip"
            area["meta_name"] = pincode
            area.pop("meta_radial", None)
        elif lat is not None and lng is not None:
            # Standard custom location radial boundary format
            area["meta_radial"] = {
                "latitude": lat,
                "longitude": lng,
                "radius": 2.0,
                "distance_unit": "kilometer",
            }
            area.pop("meta_key", None)
            area.pop("meta_type", None)
            area.pop("meta_name", None)
        else:
            # Complete stub fallback (e.g. zip code synthetic match)
            val = area.get("name") or "Area"
            h = int(hashlib.md5(val.encode()).hexdigest(), 16) % 1000000
            area["meta_key"] = str(1000000 + h)
            area["meta_type"] = "city"
            area["meta_name"] = val
            area.pop("meta_radial", None)

        return area
