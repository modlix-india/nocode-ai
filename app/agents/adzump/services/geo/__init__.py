"""Geo services orchestrator for Adzump campaigns.

Handles two-tier target area discovery:
1. Local/Hyperlocal scope: radial-grid geocoding around verified coordinates.
2. Regional/National/Global scope: LLM-driven strategic market recommendations.
"""

from __future__ import annotations

import logging
import json
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

# Config & Model Constants
GEO_TARGET_MODEL = "gpt-4o-mini"
MAX_STRATEGIC_MARKETS_TOKENS = 600
LLM_COMPLETION_TEMPERATURE = 0.2

# Geo Scopes
SCOPE_HYPERLOCAL = "hyperlocal"
SCOPE_LOCAL = "local"
SCOPE_REGIONAL = "regional"
SCOPE_NATIONAL = "national"
SCOPE_GLOBAL = "global"

ALLOWED_GEO_SCOPES = (
    SCOPE_HYPERLOCAL,
    SCOPE_LOCAL,
    SCOPE_REGIONAL,
    SCOPE_NATIONAL,
    SCOPE_GLOBAL,
)
LOCAL_GEO_SCOPES = (SCOPE_HYPERLOCAL, SCOPE_LOCAL)

# Fallback Configuration
DEFAULT_GEO_SCOPE = SCOPE_NATIONAL
FALLBACK_GEO_SCOPE = SCOPE_HYPERLOCAL
HYPERLOCAL_BUSINESS_KEYWORDS = ("dentist", "cafe", "gym", "restaurant", "clinic")
FALLBACK_TARGET_REASON = "Target area extracted from website details."


# OpenAI JSON Schema for Strategic Markets
STRATEGIC_MARKETS_SCHEMA = {
    "name": "strategic_markets",
    "schema": {
        "type": "object",
        "properties": {
            "markets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["name", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["markets"],
        "additionalProperties": False,
    },
    "strict": True,
}


def get_business_scope(product_data: dict) -> str:
    """Classify target scope from product data. Defaults to DEFAULT_GEO_SCOPE."""
    scope = (product_data.get("geo_scope") or "").strip().lower()
    if scope in ALLOWED_GEO_SCOPES:
        return scope

    # Heuristic fallback if geo_scope is missing
    business_type = (product_data.get("business_type") or "").strip().lower()
    from app.agents.adzump.tools.location import _is_real_estate

    if _is_real_estate(business_type) or any(
        kw in business_type for kw in HYPERLOCAL_BUSINESS_KEYWORDS
    ):
        return FALLBACK_GEO_SCOPE

    return DEFAULT_GEO_SCOPE


async def discover_geo_targets(
    coordinates: dict | None, product_data: dict
) -> list[dict[str, Any]]:
    """Entrypoint for Target Area Discovery.

    Returns a unified list of target area dicts:
    {
        "name": str,
        "distance_km": float | None,
        "reason": str
    }
    """
    scope = get_business_scope(product_data)
    logger.info("discover_geo_targets: resolving targets for scope=%r", scope)

    if scope in LOCAL_GEO_SCOPES:
        if not coordinates or not coordinates.get("lat") or not coordinates.get("lng"):
            logger.warning(
                "discover_geo_targets: scope is hyperlocal but no valid coordinates provided."
            )
            return []

        try:
            from app.agents.adzump.services.geo.discovery import discover_neighborhoods

            lat = float(coordinates["lat"])
            lng = float(coordinates["lng"])
            return await discover_neighborhoods(lat, lng)
        except Exception as e:
            logger.exception("discover_geo_targets: radial geocoding failed: %s", e)
            return []

    # Non-local scopes (regional, national, global) use LLM market discovery
    return await _discover_strategic_markets(product_data, scope)


async def _discover_strategic_markets(
    product_data: dict, scope: str
) -> list[dict[str, Any]]:
    """Query LLM to recommend prime targeting zones with marketing justifications."""
    from openai import AsyncOpenAI

    product_name = product_data.get("product_name") or "this product"
    business_type = product_data.get("business_type") or "business"
    summary = product_data.get("summary") or ""

    prompt = f"""You are a senior media buyer. Recommend 3 to 6 high-value geographic target regions, cities, or states for this ad campaign.
    
    PRODUCT DETAILS:
    - Name: {product_name}
    - Category: {business_type}
    - Operating Scale/Scope: {scope}
    - Business Summary: {summary}
    
    INSTRUCTIONS:
    - Based on the product's industry category, pricing, and target demographic, recommend the 3 to 6 most profitable geographic targeting locations.
    - If the scope is "global", select high-value countries or country-level hubs (e.g. "United States", "United Kingdom", "India (Tech Hubs)").
    - If the scope is "national", select prime Tier-1/Tier-2 cities or major states with high consumption intent (e.g. "Bangalore Metro", "Mumbai Metro", "Delhi NCR").
    - If the scope is "regional", select major cities or counties within the home region/state of the business.
    - Provide a short, 1-sentence strategic marketing justification for each location explaining WHY we should target this specific area (e.g. "High concentration of premium IT professionals with strong purchase intent").
    
    OUTPUT: Return ONLY valid JSON matching this exact schema:
    {{
      "markets": [
        {{"name": "Location Name", "reason": "Strategic marketing justification"}}
      ]
    }}
    """

    try:
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        response = await client.chat.completions.create(
            model=GEO_TARGET_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=LLM_COMPLETION_TEMPERATURE,
            max_tokens=MAX_STRATEGIC_MARKETS_TOKENS,
            response_format={
                "type": "json_schema",
                "json_schema": STRATEGIC_MARKETS_SCHEMA,
            },
        )

        payload = json.loads(response.choices[0].message.content)
        markets = payload.get("markets") or []

        result = []
        for m in markets:
            if m.get("name") and m.get("reason"):
                result.append(
                    {
                        "name": m["name"].strip(),
                        "distance_km": None,
                        "reason": m["reason"].strip(),
                    }
                )
        logger.info(
            "discover_strategic_markets: LLM generated %d target areas for %r",
            len(result),
            product_name,
        )
        return result

    except Exception as e:
        logger.warning(
            "discover_strategic_markets failed: %s: %s", type(e).__name__, str(e)[:200]
        )
        # Return fallback suggestions extracted during scraping if any
        suggested = product_data.get("suggested_locations") or []
        if suggested:
            return [
                {"name": s, "distance_km": None, "reason": FALLBACK_TARGET_REASON}
                for s in suggested
            ]
        return []
