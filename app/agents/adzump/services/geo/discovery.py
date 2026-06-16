"""Geo-targeting discovery service.

Classifies targeting scales and resolves targetable locations using either
radial coordinate geocoding (for local scales) or LLM recommendations (for
regional, national, or international scales).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any

from openai import AsyncOpenAI

from app.config import settings
from app.agents.adzump.adapters.google.maps import google_maps_client

logger = logging.getLogger(__name__)

# Search configuration for local radial grids
SEARCH_STEPS = [0.33, 0.66, 1.0]

# Industry-standard Constants
EARTH_RADIUS_KM = 6371.0
DEFAULT_LOCAL_RADIUS_KM = 15.0
MAX_LOCAL_NEIGHBORHOODS = 15
DEFAULT_LLM_MODEL = "gpt-4o-mini"
DEFAULT_LLM_TEMPERATURE = 0.2


def is_local_business(business_scale: str) -> bool:
    """Check if the resolved scale is local."""
    return (business_scale or "").strip().lower() == "local"


def generate_radial_offsets(
    lat: float, lng: float, radius_km: float
) -> list[tuple[float, float]]:
    """Generate coordinate sampling points in concentric rings (~49 points grid)."""
    points = [(lat, lng)]
    distances = [radius_km * step for step in SEARCH_STEPS]

    for distance in distances:
        for bearing in range(0, 360, 22):
            # Calculate offset coordinates using simple flat-earth approximation
            # suitable for small radial distances (< 50km)
            rad_bearing = math.radians(bearing)
            # 1 degree lat ~ 111km
            delta_lat = (distance * math.cos(rad_bearing)) / 111.0
            # 1 degree lng ~ 111km * cos(lat)
            delta_lng = (distance * math.sin(rad_bearing)) / (
                111.0 * math.cos(math.radians(lat))
            )
            points.append((lat + delta_lat, lng + delta_lng))

    return points


async def discover_neighborhoods(
    lat: float, lng: float, radius_km: float
) -> list[dict[str, Any]]:
    """Perform radial coordinate lookup to resolve local neighborhood names and pincodes."""
    points = generate_radial_offsets(lat, lng, radius_km)
    tasks = [google_maps_client.reverse_geocode(p[0], p[1]) for p in points]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    discovered_map = {}

    for idx, res_list in enumerate(results):
        if isinstance(res_list, Exception) or not res_list:
            continue

        center_lat, center_lng = points[idx]

        pincode = None
        pincode_place_id = None
        neighborhood = None
        city = None
        state = None
        fallback_place_id = None

        for res in res_list:
            types = res.get("types", [])
            if "postal_code" in types and not pincode:
                pincode_place_id = res.get("place_id")
                for comp in res.get("address_components", []):
                    if "postal_code" in comp.get("types", []):
                        pincode = comp.get("long_name", "").strip()
                        break

            for comp in res.get("address_components", []):
                c_types = comp.get("types", [])
                if "neighborhood" in c_types or "sublocality" in c_types:
                    if not neighborhood:
                        neighborhood = comp.get("long_name", "").strip()
                elif "locality" in c_types:
                    if not city:
                        city = comp.get("long_name", "").strip()
                elif "administrative_area_level_1" in c_types:
                    if not state:
                        state = comp.get("short_name", "").strip()

            if not fallback_place_id and (
                "neighborhood" in types or "sublocality" in types or "locality" in types
            ):
                fallback_place_id = res.get("place_id")

        target_key = pincode if pincode else (neighborhood or city)
        if not target_key:
            continue

        # Calculate actual distance from coordinates center
        # Simple Haversine distance formula
        lat1, lon1 = math.radians(lat), math.radians(lng)
        lat2, lon2 = math.radians(center_lat), math.radians(center_lng)
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        )
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        dist = round(EARTH_RADIUS_KM * c, 2)

        # Keep the coordinate representation closest to the center
        if (
            target_key not in discovered_map
            or dist < discovered_map[target_key]["distance_km"]
        ):
            boundary_place_id = pincode_place_id if pincode else fallback_place_id
            pincode_lat = center_lat
            pincode_lng = center_lng

            # Fallback geocoding if place ID is missing
            if not boundary_place_id:
                try:
                    if pincode:
                        query = f"{pincode}"
                        if city:
                            query += f", {city}"
                        if state:
                            query += f", {state}"
                        geo = await google_maps_client.geocode(query)
                    else:
                        geo = await google_maps_client.geocode(target_key)
                    if geo:
                        boundary_place_id = geo.get("place_id")
                        if geo.get("lat") is not None and geo.get("lng") is not None:
                            pincode_lat = geo["lat"]
                            pincode_lng = geo["lng"]
                except Exception:
                    pass

            discovered_map[target_key] = {
                "name": f"Pincode {pincode}" if pincode else target_key,
                "pincode": pincode or "",
                "city": city or "",
                "state": state or "",
                "lat": pincode_lat,
                "lng": pincode_lng,
                "distance_km": dist,
                "place_id": boundary_place_id,
            }

    # Sort results by distance from center
    sorted_targets = sorted(discovered_map.values(), key=lambda x: x["distance_km"])
    return sorted_targets[:MAX_LOCAL_NEIGHBORHOODS]


async def _discover_strategic_markets(
    product_data: dict, scope: str, country_code: str = "IN"
) -> list[dict[str, Any]]:
    """Query LLM to recommend prime targeting zones with marketing justifications."""
    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    product_name = product_data.get("product_name") or "Product"
    business_type = product_data.get("business_type") or "Business"
    summary = product_data.get("summary") or ""

    prompt = f"""
    Analyze this business profile and recommend target advertising locations:
    - Name: {product_name}
    - Category: {business_type}
    - Operating Scope/Scale: {scope}
    - Target Country: {country_code}
    - Business Summary: {summary}
    
    INSTRUCTIONS:
    - Based on the operating scale, recommend the 3 to 6 most profitable geographic targeting locations.
    - All locations must be within {country_code}.
    - If the scope is "global" or "international", select high-value country level target nodes.
    - If the scope is "national", select prime Tier-1/Tier-2 cities or major states with high consumption intent within {country_code}.
    - If the scope is "regional", select major cities or counties within the home region/state of the business.
    
    Return your response ONLY as a JSON block with the following schema:
    {{
      "locations": [
        {{
          "name": "Location Name (e.g. Bengaluru, Karnataka, India)",
          "type": "city" // "city", "state", or "country"
        }}
      ]
    }}
    """

    try:
        response = await client.chat.completions.create(
            model=DEFAULT_LLM_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "You are a senior marketing strategist who output clean, valid JSON responses only.",
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=DEFAULT_LLM_TEMPERATURE,
        )
        content = response.choices[0].message.content
        data = json.loads(content)
        locations = data.get("locations") or []

        # Geocode the LLM recommended locations in parallel to obtain lat/lng and place IDs
        tasks = [google_maps_client.geocode(loc["name"]) for loc in locations]
        geocode_results = await asyncio.gather(*tasks, return_exceptions=True)

        final_targets = []
        for idx, loc in enumerate(locations):
            geo = geocode_results[idx]
            if isinstance(geo, Exception) or not geo:
                continue

            final_targets.append(
                {
                    "name": loc["name"],
                    "pincode": geo.get("pincode") or "",
                    "city": geo.get("city") or "",
                    "state": geo.get("state") or "",
                    "lat": geo["lat"],
                    "lng": geo["lng"],
                    "distance_km": 0.0,
                    "place_id": geo["place_id"],
                }
            )

        return final_targets

    except Exception as e:
        logger.exception("LLM strategic market discovery failed: %s", e)
        return []


async def discover_geo_targets(
    coordinates: dict | None, product_data: dict, country_code: str = "IN"
) -> list[dict[str, Any]]:
    """Core entrypoint for geocoding scale classification and location discovery."""
    scale = (product_data.get("business_scale") or "national").strip().lower()

    if is_local_business(scale):
        if coordinates and "lat" in coordinates and "lng" in coordinates:
            # Local radius neighborhood grid scan
            return await discover_neighborhoods(
                coordinates["lat"],
                coordinates["lng"],
                radius_km=DEFAULT_LOCAL_RADIUS_KM,
            )
        else:
            logger.warning(
                "Local business targeting requested but no coordinates provided."
            )
            return []

    # Non-local operating scales (regional, national, international) query the LLM
    return await _discover_strategic_markets(product_data, scale, country_code)
