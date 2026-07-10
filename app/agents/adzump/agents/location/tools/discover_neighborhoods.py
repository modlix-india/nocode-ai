"""discover_neighborhoods - LLM-callable tool for the LOCAL targeting path.

Radial grid scan around the confirmed business pin: reverse-geocode ~136 points
in concentric rings, dedupe by pincode/neighborhood, keep the closest ≤25.
The scan itself is pure geometry + Google Maps calls - the LLM only decides
*to* call this tool (local / real-estate businesses); it never supplies
coordinates (they're read from session state, so the model can't invent them).
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.adapters.google.maps import google_maps_client
from app.agents.adzump.agents.location.tools._shared import finalize_targets

logger = logging.getLogger(__name__)

# Search configuration for local radial grids
SEARCH_STEPS = [0.33, 0.66, 1.0]

# Geometry + scan-bound constants
EARTH_RADIUS_KM = 6371.0
DEFAULT_LOCAL_RADIUS_KM = 8.0
MAX_LOCAL_NEIGHBORHOODS = 25


async def _discover_neighborhoods(params: dict, context: dict) -> ToolResult:
    """Tool execute: scan around the confirmed pin, then finalize (map/persist/render)."""
    session_ctx = context.get("session_context") or {}
    place = (session_ctx.get("product_data") or {}).get("place") or {}

    lat = place.get("lat")
    lng = place.get("lng")
    if lat is None or lng is None:
        return ToolResult(
            success=False,
            error=(
                "No confirmed business coordinates in session state - the location "
                "must be geocoded before neighborhoods can be scanned. Report this "
                "to the caller; do not guess coordinates."
            ),
        )

    radius_km = float(params.get("radius_km") or DEFAULT_LOCAL_RADIUS_KM)
    resolved = await scan_neighborhoods(float(lat), float(lng), radius_km)
    if not resolved:
        return ToolResult(
            success=False,
            error=f"Radial scan found no neighborhoods within {radius_km} km.",
        )

    mapped = await finalize_targets(resolved, context)
    names = [a.get("name", "") for a in mapped if a.get("name")]
    return ToolResult(
        success=True,
        data={"count": len(mapped), "locations": names},
        summary=(
            f"Scanned {radius_km:g} km around the business pin: {len(mapped)} "
            f"neighborhoods mapped and saved ({', '.join(names[:6])}"
            f"{'…' if len(names) > 6 else ''})."
        ),
    )


async def scan_neighborhoods(
    lat: float, lng: float, radius_km: float
) -> list[dict[str, Any]]:
    """Perform radial coordinate lookup to resolve local neighborhood names and pincodes."""
    points = _generate_radial_offsets(lat, lng, radius_km)
    tasks = [google_maps_client.reverse_geocode(p[0], p[1]) for p in points]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    discovered_map = {}

    for idx, res_list in enumerate(results):
        if isinstance(res_list, Exception) or not res_list:
            continue

        center_lat, center_lng = points[idx]

        pincode = None
        pincode_place_id = None
        pincode_centroid_lat: float | None = None
        pincode_centroid_lng: float | None = None
        neighborhood = None
        city = None
        state = None
        fallback_place_id = None

        for res in res_list:
            types = res.get("types", [])
            if "postal_code" in types and not pincode:
                pincode_place_id = res.get("place_id")
                geom_loc = res.get("geometry", {}).get("location", {})
                if geom_loc.get("lat") is not None and geom_loc.get("lng") is not None:
                    pincode_centroid_lat = geom_loc["lat"]
                    pincode_centroid_lng = geom_loc["lng"]
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

        # Prefer the pincode centroid from the geocoder response; fall back to grid point
        ref_lat = pincode_centroid_lat if pincode_centroid_lat is not None else center_lat
        ref_lng = pincode_centroid_lng if pincode_centroid_lng is not None else center_lng
        dist = _haversine_km(lat, lng, ref_lat, ref_lng)

        # Keep the closest representative coordinate for each unique pincode
        if (
            target_key not in discovered_map
            or dist < discovered_map[target_key]["distance_km"]
        ):
            boundary_place_id = pincode_place_id if pincode else fallback_place_id
            pincode_lat = ref_lat
            pincode_lng = ref_lng

            # Fallback geocoding only when place ID is missing
            if not boundary_place_id:
                try:
                    if pincode:
                        query = pincode
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
                            dist = _haversine_km(lat, lng, pincode_lat, pincode_lng)
                            # TODO: re-check dist against the incumbent - the
                            # fallback can move the point past the entry it beat.
                except Exception:
                    pass  # suppress: fallback geocode is optional enrichment; keep the grid-point area

            display_name = neighborhood or (f"Pincode {pincode}" if pincode else target_key)
            discovered_map[target_key] = {
                "name": display_name,
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


def _generate_radial_offsets(
    lat: float, lng: float, radius_km: float
) -> list[tuple[float, float]]:
    """Generate coordinate sampling points in concentric rings (~136-point grid: 1 center + 3 rings × 45 bearings)."""
    points = [(lat, lng)]
    distances = [radius_km * step for step in SEARCH_STEPS]

    for distance in distances:
        for bearing in range(0, 360, 8):
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


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return the great-circle distance in km between two GPS coordinates."""
    rlat1, rlng1 = math.radians(lat1), math.radians(lng1)
    rlat2, rlng2 = math.radians(lat2), math.radians(lng2)
    dlat, dlng = rlat2 - rlat1, rlng2 - rlng1
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2
    return round(EARTH_RADIUS_KM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 2)


discover_neighborhoods_tool = ToolDefinition(
    name="discover_neighborhoods",
    description=(
        "Scan the area around the business's confirmed map pin and resolve nearby "
        "neighborhoods/pincodes as targeting areas. Use for LOCAL businesses "
        "(real estate, physical stores). Coordinates come from session state - "
        "never pass them. Mapping to the ad platform, persistence, and the map "
        "re-render happen automatically."
    ),
    display_name="Scan Neighborhoods",
    parameters=[
        ToolParameter(
            name="radius_km",
            type="number",
            description=f"Scan radius in km (default {DEFAULT_LOCAL_RADIUS_KM:g}).",
            required=False,
        ),
    ],
    execute=_discover_neighborhoods,
)
