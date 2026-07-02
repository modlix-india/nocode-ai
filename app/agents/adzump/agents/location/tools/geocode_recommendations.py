"""geocode_recommendations — LLM-callable tool for the BROAD targeting path.

The location agent's LLM reasons about which markets to target (3–6 cities/
states/countries) and passes them here as structured input — the tool schema
IS the structured output, so there is no JSON-in-prose parsing. The tool then
geocodes each market, tags it with the picked scale (which drives Meta
location_type resolution), and finalizes (map / persist / re-render).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.adapters.google.maps import google_maps_client
from app.agents.adzump.agents.location.tools._shared import finalize_targets

logger = logging.getLogger(__name__)

_VALID_TYPES = {"city", "state", "country"}


async def _geocode_recommendations(params: dict, context: dict) -> ToolResult:
    """Tool execute: geocode the picked markets, then finalize (map/persist/render)."""
    locations = params.get("locations") or []
    if not isinstance(locations, list) or not locations:
        return ToolResult(
            success=False,
            error="Provide `locations`: a list of {name, type} market picks.",
        )

    picks: list[dict[str, str]] = []
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        name = (loc.get("name") or "").strip()
        loc_type = (loc.get("type") or "").strip().lower()
        if not name:
            continue
        if loc_type not in _VALID_TYPES:
            loc_type = "city"  # tolerate a stray type rather than dropping a market
        picks.append({"name": name, "type": loc_type})
    if not picks:
        return ToolResult(
            success=False,
            error="No usable locations — every entry needs a non-empty `name`.",
        )

    # Geocode the recommended locations in parallel to obtain lat/lng and place IDs.
    tasks = [google_maps_client.geocode(p["name"]) for p in picks]
    geocode_results = await asyncio.gather(*tasks, return_exceptions=True)

    resolved: list[dict[str, Any]] = []
    skipped: list[str] = []
    for pick, geo in zip(picks, geocode_results):
        if isinstance(geo, Exception) or not geo:
            skipped.append(pick["name"])
            continue
        resolved.append(
            {
                "name": pick["name"],
                # Keep the scale the strategist picked (city/state/country) so
                # platform mapping resolves it as the right Meta location_type
                # instead of always searching it as a city.
                "scale": pick["type"],
                "pincode": geo.get("pincode") or "",
                "city": geo.get("city") or "",
                "state": geo.get("state") or "",
                "lat": geo["lat"],
                "lng": geo["lng"],
                "distance_km": 0.0,
                "place_id": geo["place_id"],
            }
        )

    if not resolved:
        return ToolResult(
            success=False,
            error=(
                "None of the picked markets could be geocoded "
                f"({', '.join(skipped)}). Try more specific names "
                '(e.g. "Bengaluru, Karnataka, India").'
            ),
        )

    mapped = await finalize_targets(resolved, context)
    names = [a.get("name", "") for a in mapped if a.get("name")]
    summary = (
        f"{len(mapped)} markets geocoded, mapped, and saved: {', '.join(names)}."
    )
    if skipped:
        summary += f" Skipped (geocode failed): {', '.join(skipped)}."
    return ToolResult(
        success=True,
        data={"count": len(mapped), "locations": names, "skipped": skipped},
        summary=summary,
    )


geocode_recommendations_tool = ToolDefinition(
    name="geocode_recommendations",
    description=(
        "Turn your picked target markets into resolved targeting areas. Use for "
        "BROAD businesses (regional / national / international) after you've "
        "reasoned about the 3-6 best markets. Each entry: the market's name "
        '(qualified, e.g. "Bengaluru, Karnataka, India") and its type. '
        "Geocoding, ad-platform mapping, persistence, and the map re-render "
        "happen automatically."
    ),
    display_name="Geocode Markets",
    parameters=[
        ToolParameter(
            name="locations",
            type="array",
            description="The 3-6 target markets you picked.",
            required=True,
            items={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": 'Qualified market name, e.g. "Bengaluru, Karnataka, India".',
                    },
                    "type": {
                        "type": "string",
                        "enum": ["city", "state", "country"],
                        "description": "The geographic level of this market.",
                    },
                },
                "required": ["name", "type"],
            },
        ),
    ],
    execute=_geocode_recommendations,
)
