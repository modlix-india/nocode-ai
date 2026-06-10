"""Radial neighborhood grid geocoding scanner.

Generates coordinate offsets surrounding a central point, reverse-geocodes
each using Google Maps Geocoding API to discover targetable local places,
calculates distances, deduplicates, and returns structured targets.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from app.agents.adzump.adapters.google.maps import google_maps_client

logger = logging.getLogger(__name__)

# Search Radius & Mathematical Constants
SEARCH_RADIUS_KM = 15.0
EARTH_RADIUS_KM = 6371.0
COMPASS_BEARINGS = (0, 45, 90, 135, 180, 225, 270, 315)  # 8 direction headings
SEARCH_STEPS = (0.33, 0.66, 1.0)  # Samples at exactly 5km, 10km, and 15km

# Geocoding Config & Priorities
NEIGHBORHOOD_ADDRESS_TYPES = (
    "sublocality_level_1",
    "neighborhood",
    "sublocality_level_2",
    "sublocality",
)
MAX_GEOCODE_CANDIDATES = 2
MAX_DISTANCE_MULTIPLIER = 1.15
MAX_DISCOVERED_NEIGHBORHOODS = 18

# Network Config
HTTP_TIMEOUT_SECONDS = 10.0


def calculate_distance(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Haversine formula to compute exact distance in km between two coordinate points."""
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lng / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return round(EARTH_RADIUS_KM * c, 1)


def generate_radial_offsets(
    lat: float, lng: float, radius_km: float = SEARCH_RADIUS_KM
) -> list[tuple[float, float]]:
    """Generates coordinate sampling points at 8 compass bearings.

    Samples at 0.5 * radius_km and 1.0 * radius_km, resulting in a perfect radial grid
    distribution (1 center + 8 midway + 8 outer = 17 points).
    """
    points = [(lat, lng)]
    distances = [radius_km * step for step in SEARCH_STEPS]

    lat_rad = math.radians(lat)
    lng_rad = math.radians(lng)

    for d in distances:
        for b in COMPASS_BEARINGS:
            b_rad = math.radians(b)

            # Destination coordinate calculations using Haversine formulas
            new_lat_rad = math.asin(
                math.sin(lat_rad) * math.cos(d / EARTH_RADIUS_KM)
                + math.cos(lat_rad) * math.sin(d / EARTH_RADIUS_KM) * math.cos(b_rad)
            )
            new_lng_rad = lng_rad + math.atan2(
                math.sin(b_rad) * math.sin(d / EARTH_RADIUS_KM) * math.cos(lat_rad),
                math.cos(d / EARTH_RADIUS_KM)
                - math.sin(lat_rad) * math.sin(new_lat_rad),
            )

            points.append((math.degrees(new_lat_rad), math.degrees(new_lng_rad)))

    return points


def _extract_address_details(components: list[dict[str, Any]]) -> dict[str, str | None]:
    """Extract neighborhood, pincode, city, and state from address components."""
    details = {
        "neighborhood": None,
        "pincode": None,
        "city": None,
        "state": None
    }

    # 1. Extract neighborhood name
    for t in NEIGHBORHOOD_ADDRESS_TYPES:
        for comp in components:
            if t in comp.get("types", []):
                long_name = comp.get("long_name")
                if long_name:
                    details["neighborhood"] = long_name.strip()
                    break
        if details["neighborhood"]:
            break

    # 2. Extract postcode, city, and state
    for comp in components:
        types = comp.get("types", [])
        if "postal_code" in types:
            details["pincode"] = comp.get("long_name", "").strip()
        elif "locality" in types:
            details["city"] = comp.get("long_name", "").strip()
        elif "administrative_area_level_1" in types:
            details["state"] = comp.get("short_name", "").strip()

    return details


async def discover_neighborhoods(
    center_lat: float, center_lng: float, radius_km: float = SEARCH_RADIUS_KM
) -> list[dict[str, Any]]:
    """Generates surrounding grid coordinates, geocodes actual neighborhood names,
    calculates distance offsets, deduplicates, and returns structured targeting cards.
    """
    if not google_maps_client.api_key:
        logger.warning(
            "discover_neighborhoods skipped: Google Maps API key is missing."
        )
        return []

    points = generate_radial_offsets(center_lat, center_lng, radius_km)
    logger.info(
        "discover_neighborhoods: sampling %d radial coordinates around lat=%f lng=%f",
        len(points),
        center_lat,
        center_lng,
    )

    discovered_map: dict[str, dict[str, Any]] = {}

    for idx, (lat, lng) in enumerate(points):
        results = await google_maps_client.reverse_geocode(lat, lng)
        if not results:
            continue

        # Check top-level result components to extract locality/neighborhood names
        for res in results[
            :MAX_GEOCODE_CANDIDATES
        ]:  # Inspect first few candidates for accuracy
            components = res.get("address_components") or []
            addr_details = _extract_address_details(components)
            name = addr_details["neighborhood"]
            if not name:
                continue

            # Retrieve coordinates of the matched locality center
            loc_geom = res.get("geometry", {}).get("location") or {}
            res_lat = loc_geom.get("lat")
            res_lng = loc_geom.get("lng")

            if res_lat is not None and res_lng is not None:
                dist = calculate_distance(center_lat, center_lng, res_lat, res_lng)
                final_lat = res_lat
                final_lng = res_lng
            else:
                dist = calculate_distance(center_lat, center_lng, lat, lng)
                final_lat = lat
                final_lng = lng

            # Filter out anything too far away
            if dist > radius_km * MAX_DISTANCE_MULTIPLIER:
                continue

            # Deduplication: Keep the coordinate representation closest to the center
            if name not in discovered_map or dist < discovered_map[name]["distance_km"]:
                discovered_map[name] = {
                    "name": name,
                    "pincode": addr_details["pincode"],
                    "city": addr_details["city"],
                    "state": addr_details["state"],
                    "lat": final_lat,
                    "lng": final_lng,
                    "distance_km": dist,
                    "place_id": res.get("place_id"),
                    "reason": f"High-intent local neighborhood within {dist} km radius of your confirmed location."
                    if dist > 0
                    else "Primary target locality containing your confirmed location.",
                }

    # Format, sort, and select top closest discovered neighborhoods
    sorted_targets = sorted(discovered_map.values(), key=lambda x: x["distance_km"])
    logger.info(
        "discover_neighborhoods: successfully resolved %d unique neighborhoods",
        len(sorted_targets),
    )
    return sorted_targets[:MAX_DISCOVERED_NEIGHBORHOODS]
