"""How the business is described to the keyword agent.

Shared by both entries — the orchestrator builds it for a fresh run, ``handle()`` rebuilds it
from the saved session for an edit — so what the agent knows can't drift between them.
"""

from __future__ import annotations

from app.agents.adzump.agents.campaign.google.keyword.constants import MAX_SERVICE_AREAS


def business_text(product: dict) -> str:
    """The business the keywords must target."""
    parts = [
        f"Business: {product.get('product_name', '')}",
        f"Type: {product.get('business_type', '')}",
    ]
    if features := product.get("unique_features"):
        parts.append("Offerings / USPs: " + "; ".join(str(f) for f in features))
    if services := product.get("products_services"):
        parts.append("Products / services: " + "; ".join(str(s) for s in services))
    if summary := product.get("summary"):
        parts.append("Summary: " + str(summary))
    return "\n".join(p for p in parts if p.strip())


def conversation_text(session_ctx: dict) -> str:
    """business_text plus what a *question* may reach for that generation never needs.

    A build targets the offering; a question can be about the competition or the budget
    ("are we covering what competitors bid on?"), so those are added here rather than
    widening the build prompt.
    """
    product = session_ctx.get("product_data") or {}
    parts = [business_text(product)]

    # analyze_competitors' shape (tools/competitor.py): {"competitors": [{"name": ...}]}
    competitive = session_ctx.get("competitor_analysis") or {}
    names = [
        str(c.get("name")).strip()
        for c in (competitive.get("competitors") or [])
        if isinstance(c, dict) and c.get("name")
    ]
    if names:
        parts.append("Competitors: " + ", ".join(names))

    if budget := (session_ctx.get("campaign_spec") or {}).get("budget"):
        parts.append(f"Daily budget: {budget}")
    return "\n".join(p for p in parts if p.strip())


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


def resolve_location(product: dict, is_location_specific: bool) -> tuple[str, list[str]]:
    """City primary + service areas from target_areas (the location field is often empty).
    Local-vs-national comes from the taxonomy, not string-matched business_scale."""
    if not is_location_specific:
        return "", []
    areas = [a for a in (product.get("target_areas") or []) if isinstance(a, dict)]
    names = [str(a.get("name")).strip() for a in areas if a.get("name")]
    city = next((str(a.get("city")).strip() for a in areas if a.get("city")), "")
    primary = _location_text(product) or city or (names[0] if names else "")
    service_areas = [n for n in names if n and n.lower() != primary.lower()][:MAX_SERVICE_AREAS]
    return primary, service_areas
