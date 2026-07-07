"""Geo-targeting autocomplete and search service.

Provides lookup/autocomplete suggestions for target ad networks (Google Ads, Meta Ads)
and geocodes candidate coordinates and place details in parallel.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.agents.adzump.adapters.connections import TokenServiceError
from app.agents.adzump.adapters.google.maps import google_maps_client
from app.agents.adzump.adapters.meta.client import meta_client

logger = logging.getLogger(__name__)


def _geo_to_candidate(s_data: dict[str, Any], geo: dict | Exception | None) -> dict[str, Any]:
    """Convert a geocode result + platform suggestion into a unified candidate dict."""
    lat = lng = place_id = pincode = city = state = address = None
    if geo and not isinstance(geo, Exception):
        lat = geo.get("lat")
        lng = geo.get("lng")
        place_id = geo.get("place_id")
        pincode = geo.get("pincode")
        city = geo.get("city")
        state = geo.get("state")
        address = geo.get("address")
    return {
        "id": s_data["id"],
        "name": s_data["name"],
        "canonical_name": address or s_data["canonical_name"],
        "type": s_data["type"],
        "lat": lat,
        "lng": lng,
        "place_id": place_id,
        "pincode": pincode or "",
        "city": city or "",
        "state": state or "",
    }


async def search_autocomplete_locations(
    q: str,
    platform: str,
    client_code: str,
    auth_headers: dict[str, str],
    session_context: dict[str, Any],
    country_code: str = "IN",
) -> list[dict[str, Any]]:
    """Query location autocomplete options for the ad network and resolve place coordinates."""
    if not q or len(q.strip()) < 2:
        return []

    q = q.strip()
    platform = platform.lower().strip()
    candidates: list[dict[str, Any]] = []

    if platform == "google":
        try:
            from app.agents.adzump.adapters.google.client import google_ads_client

            res = await google_ads_client.suggest_geo_targets(
                q, country_code, client_code, auth_headers
            )
            suggestions = res.get("geoTargetConstantSuggestions") or []
            names_to_geocode: list[str] = []
            suggest_data: list[dict[str, Any]] = []

            for sug in suggestions:
                constant = sug.get("geoTargetConstant") or {}
                name = constant.get("canonicalName") or constant.get("name")
                if not name:
                    continue

                names_to_geocode.append(name)
                suggest_data.append({
                    "id": constant.get("id"),
                    "name": constant.get("name"),
                    "canonical_name": name,
                    "type": constant.get("targetType") or "Location",
                })

            # Fetch geocoding in parallel for lat/lng/pincode/city/state
            tasks = [google_maps_client.geocode(name) for name in names_to_geocode]
            geocode_results = await asyncio.gather(*tasks, return_exceptions=True)

            for idx, geo in enumerate(geocode_results):
                candidates.append(_geo_to_candidate(suggest_data[idx], geo))
        except TokenServiceError:
            # Auth outage is NOT "no matches" - let the route answer 503, or
            # the UI renders a broken search as a confident empty list.
            raise
        except Exception as e:
            logger.exception("Google Ads geolocations suggest autocomplete failed: %s", e)

    elif platform == "meta":
        try:
            params = {
                "type": "adgeolocation",
                "q": q,
                "location_types": json.dumps(["zip", "city", "neighborhood", "region"]),
                "country_code": country_code,
            }
            res = await meta_client.get(
                "/search", client_code, auth_headers, params=params
            )
            data = res.get("data") or []
            names_to_geocode = []
            suggest_data = []

            for item in data:
                name = item.get("name")
                if not name:
                    continue

                c_name = f"{name}, {item.get('country_code', country_code)}"
                names_to_geocode.append(c_name)
                suggest_data.append({
                    "id": item.get("key"),
                    "name": name,
                    "canonical_name": name,
                    "type": item.get("type") or "Location",
                })

            # Fetch geocoding in parallel
            tasks = [google_maps_client.geocode(name) for name in names_to_geocode]
            geocode_results = await asyncio.gather(*tasks, return_exceptions=True)

            for idx, geo in enumerate(geocode_results):
                candidates.append(_geo_to_candidate(suggest_data[idx], geo))
        except TokenServiceError:
            raise  # same contract as the google branch above
        except Exception as e:
            logger.exception("Meta Ads geolocations search failed: %s", e)

    return candidates
