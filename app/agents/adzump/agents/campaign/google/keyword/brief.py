"""Where the keyword agent seeds from — the geo half of the brief.

The business half is channel-neutral and lives in ``agents/campaign/brief.py``. What stays
here is keyword-shaped: the service-area cap exists for the seed prompt, and
``is_location_specific`` comes from the keyword taxonomy.
"""

from __future__ import annotations

from app.agents.adzump.agents.campaign.google.keyword.constants import MAX_SERVICE_AREAS


def _location_text(product: dict) -> str:
    loc = product.get("location")
    if isinstance(loc, str):
        return loc.strip()
    if isinstance(loc, dict):
        return (
            loc.get("area_location")
            or loc.get("product_location")
            or loc.get("location")
            or ""
        ).strip()
    return ""


def resolve_location(
    product: dict, is_location_specific: bool
) -> tuple[str, list[str]]:
    """City primary + service areas from target_areas (the location field is often empty).
    Local-vs-national comes from the taxonomy, not string-matched business_scale."""
    if not is_location_specific:
        return "", []
    areas = [a for a in (product.get("target_areas") or []) if isinstance(a, dict)]
    names = [str(a.get("name")).strip() for a in areas if a.get("name")]
    city = next((str(a.get("city")).strip() for a in areas if a.get("city")), "")
    primary = _location_text(product) or city or (names[0] if names else "")
    service_areas = [n for n in names if n and n.lower() != primary.lower()][
        :MAX_SERVICE_AREAS
    ]
    return primary, service_areas
