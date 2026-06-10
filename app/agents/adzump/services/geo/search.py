"""Geolocation search and autocomplete service.

Handles queries to external advertising platforms (Google Ads, Meta) to fetch targeting
suggestions and geocodes them in parallel to obtain lat/lng coordinates and Place IDs.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

from app.agents.adzump.adapters.google.client import google_ads_client
from app.agents.adzump.adapters.google.maps import google_maps_client
from app.agents.adzump.adapters.meta.client import meta_client

logger = logging.getLogger(__name__)


async def search_autocomplete_locations(
    q: str,
    platform: str,
    client_code: str,
    auth_headers: dict[str, str],
    session_context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Searches targeting locations autocomplete suggestion list for Google Ads and Meta platforms,
    and resolves their geographic coordinates/place_id in parallel.
    """
    if len(q.strip()) < 2:
        return []

    platform = platform.lower().strip()
    candidates: list[dict[str, Any]] = []

    if platform == "google":
        try:
            spec = session_context.get("campaign_spec") or {}
            customer_id = spec.get("account") or spec.get("parent_account")

            res = await google_ads_client.post(
                "geoTargetConstants:suggest",
                client_code,
                auth_headers,
                {"locale": "en", "countryCode": "IN", "locationNames": {"names": [q]}},
            )
            suggestions = res.get("geoTargetConstantSuggestions") or []
            names_to_geocode = []
            suggest_data = []
            for s in suggestions:
                g_const = s.get("geoTargetConstant") or {}
                c_name = g_const.get("canonicalName") or g_const.get("name")
                names_to_geocode.append(c_name)
                suggest_data.append(
                    {
                        "id": g_const.get("id"),
                        "name": g_const.get("name"),
                        "type": g_const.get("targetType"),
                        "canonical_name": c_name,
                    }
                )

            tasks = [google_maps_client.geocode(name) for name in names_to_geocode]
            geocode_results = await asyncio.gather(*tasks, return_exceptions=True)

            for i, geo in enumerate(geocode_results):
                if isinstance(geo, Exception) or not geo:
                    lat, lng, place_id = 12.9716, 77.5946, None
                else:
                    lat = geo.get("lat", 12.9716)
                    lng = geo.get("lng", 77.5946)
                    place_id = geo.get("place_id")

                s_data = suggest_data[i]
                candidates.append(
                    {
                        "id": s_data["id"],
                        "name": s_data["name"],
                        "type": s_data["type"],
                        "canonical_name": s_data["canonical_name"],
                        "lat": lat,
                        "lng": lng,
                        "place_id": place_id,
                    }
                )
        except Exception as e:
            logger.exception("Google autocomplete failed")
            raise RuntimeError(f"Google Ads API autocomplete failed: {e}") from e

    elif platform == "meta":
        try:
            import json
            params = {
                "type": "adgeolocation",
                "q": q,
                "location_types": json.dumps(["zip", "city", "neighborhood", "region"]),
                "country_code": "IN",
            }
            res = await meta_client.get(
                "/search", client_code, auth_headers, params=params
            )
            data = res.get("data") or []
            names_to_geocode = []
            suggest_data = []
            for item in data:
                name = item.get("name")
                c_name = f"{name}, {item.get('country_code', 'IN')}"
                names_to_geocode.append(c_name)
                suggest_data.append(
                    {
                        "id": item.get("key"),
                        "name": name,
                        "type": item.get("type"),
                        "canonical_name": c_name,
                    }
                )

            tasks = [google_maps_client.geocode(name) for name in names_to_geocode]
            geocode_results = await asyncio.gather(*tasks, return_exceptions=True)

            for i, geo in enumerate(geocode_results):
                if isinstance(geo, Exception) or not geo:
                    lat, lng, place_id = 12.9716, 77.5946, None
                else:
                    lat = geo.get("lat", 12.9716)
                    lng = geo.get("lng", 77.5946)
                    place_id = geo.get("place_id")

                s_data = suggest_data[i]
                candidates.append(
                    {
                        "id": s_data["id"],
                        "name": s_data["name"],
                        "type": s_data["type"],
                        "canonical_name": s_data["canonical_name"],
                        "lat": lat,
                        "lng": lng,
                        "place_id": place_id,
                    }
                )
        except Exception as e:
            logger.exception("Meta autocomplete failed")
            raise RuntimeError(f"Meta Graph API autocomplete failed: {e}") from e

    return candidates
