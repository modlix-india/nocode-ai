"""Prompts for the geo-targeting strategist — the ONE model decision in the
geo subsystem (everything else in services/geo is deterministic).

Lifted out of discovery.py so the prompt is first-class: reviewable on its
own, and the natural home for future enrichment (more business context,
few-shot examples) without touching pipeline code.
"""

from __future__ import annotations

STRATEGIST_SYSTEM_PROMPT = (
    "You are a senior marketing strategist. Output clean, valid JSON only — "
    "no markdown, no explanation."
)


def build_strategic_markets_prompt(
    product_name: str,
    business_type: str,
    scope: str,
    country_code: str,
    summary: str,
) -> str:
    """User prompt asking for 3-6 profitable targeting locations at the right
    geographic level for the business's operating scope."""
    return f"""
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
    - Each location's "type" MUST be exactly one of: "city", "state", or "country".

    Return ONLY a JSON object (no markdown fences, no prose) of this exact shape:
    {{"locations": [{{"name": "<Location, Parent, Country>", "type": "<city|state|country>"}}]}}

    Example for scope=national, country=IN:
    {{"locations": [{{"name": "Bengaluru, Karnataka, India", "type": "city"}}, {{"name": "Maharashtra, India", "type": "state"}}]}}
    """
