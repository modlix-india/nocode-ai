"""Geo-targeting autocomplete and search service.

Provides lookup/autocomplete suggestions for target ad networks (Google Ads,
Meta Ads): one platform lookup, then parallel geocoding to attach coordinates
and place details to each candidate.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.agents.adzump.platform import is_google, is_meta
from app.agents.adzump.adapters.connections import TokenServiceError
from app.agents.adzump.adapters.google.maps import google_maps_client
from app.agents.adzump.adapters.meta.client import meta_client

logger = logging.getLogger(__name__)


async def search_autocomplete_locations(
    q: str,
    platform: str,
    client_code: str,
    auth_headers: dict[str, str],
    country_code: str = "IN",
) -> list[dict[str, Any]]:
    """Query location autocomplete options for the ad network and resolve place coordinates."""
    if not q or len(q.strip()) < 2:
        return []
    q = q.strip()
    if is_google(platform):
        return await _search_google(q, client_code, auth_headers, country_code)
    if is_meta(platform):
        return await _search_meta(q, client_code, auth_headers, country_code)
    return []


async def _search_google(
    q: str, client_code: str, auth_headers: dict[str, str], country_code: str
) -> list[dict[str, Any]]:
    """Google Ads geo-target suggest → geocoded candidates."""
    try:
        from app.agents.adzump.adapters.google.client import google_ads_client

        response = await google_ads_client.suggest_geo_targets(
            q, country_code, client_code, auth_headers
        )
        picks: list[tuple[str, dict[str, Any]]] = []
        for suggestion in response.get("geoTargetConstantSuggestions") or []:
            constant = suggestion.get("geoTargetConstant") or {}
            name = constant.get("canonicalName") or constant.get("name")
            if not name:
                continue
            picks.append((name, {
                "id": constant.get("id"),
                "name": constant.get("name"),
                "canonical_name": name,
                "type": constant.get("targetType") or "Location",
            }))
        return await _geocode_candidates(picks)
    except TokenServiceError:
        # Auth outage is NOT "no matches" - let the route answer 503, or
        # the UI renders a broken search as a confident empty list.
        raise
    except Exception as e:
        logger.exception("Google Ads geolocations suggest autocomplete failed: %s", e)
        return []


async def _search_meta(
    q: str, client_code: str, auth_headers: dict[str, str], country_code: str
) -> list[dict[str, Any]]:
    """Meta adgeolocation search → geocoded candidates."""
    try:
        search_params = {
            "type": "adgeolocation",
            "q": q,
            "location_types": json.dumps(["zip", "city", "neighborhood", "region"]),
            "country_code": country_code,
        }
        response = await meta_client.get(
            "/search", client_code, auth_headers, params=search_params
        )
        picks: list[tuple[str, dict[str, Any]]] = []
        for item in response.get("data") or []:
            name = item.get("name")
            if not name:
                continue
            geocode_query = f"{name}, {item.get('country_code', country_code)}"
            picks.append((geocode_query, {
                "id": item.get("key"),
                "name": name,
                "canonical_name": name,
                "type": item.get("type") or "Location",
            }))
        return await _geocode_candidates(picks)
    except TokenServiceError:
        raise  # same contract as the google branch above
    except Exception as e:
        logger.exception("Meta Ads geolocations search failed: %s", e)
        return []


async def _geocode_candidates(
    picks: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Geocode each (query, suggestion) pick in parallel and compose the
    unified candidates. Per-item geocode failures degrade to a candidate
    without coordinates - enrichment noise never drops a suggestion."""
    geocode_results = await asyncio.gather(
        *(google_maps_client.geocode(query) for query, _ in picks),
        return_exceptions=True,
    )
    return [
        _geo_to_candidate(suggestion, geo)
        for (_, suggestion), geo in zip(picks, geocode_results)
    ]


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
