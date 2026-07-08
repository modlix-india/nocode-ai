"""Shared geo / taxonomy / business-text helpers for keyword research.

Pure functions over `product`/`session_ctx` with no coupling to any single
campaign-tool call site. Used by both the Google Search brand+generic
keyword_research tool (`agents/campaign/tools/google/keyword_research.py`)
and the competitor keyword research tool
(`app/agents/adzump/tools/competitor_keywords.py`).
"""

from __future__ import annotations

import logging
import re

from app.agents.adzump.adapters.google import keyword_planner
from app.agents.adzump.adapters.google.client import google_ads_client
from app.agents.adzump.adapters.google.maps import google_maps_client
from app.agents.adzump.agents.keyword.models import BusinessProfile

logger = logging.getLogger(__name__)


async def resolve_country_geo_target(
    country_code: str,
    customer_id: str,
    login_customer_id: str,
    context: dict,
) -> str:
    """GAQL lookup for the country-level geo target constant. Falls back to India."""
    if not country_code or not customer_id:
        return keyword_planner.INDIA_GEO_TARGET
    if not re.fullmatch(r"[A-Za-z]{2}", country_code):
        logger.warning(
            "geo_target: invalid country_code=%r; India fallback", country_code
        )
        return keyword_planner.INDIA_GEO_TARGET
    safe_code = country_code.upper()
    try:
        rows = await google_ads_client.search(
            query=(
                "SELECT geo_target_constant.resource_name "
                "FROM geo_target_constant "
                f"WHERE geo_target_constant.country_code = '{safe_code}' "
                "  AND geo_target_constant.target_type = 'Country' "
                "LIMIT 1"
            ),
            customer_id=customer_id,
            login_customer_id=login_customer_id,
            client_code=context.get("client_code", ""),
            auth_headers=context.get("headers", {}),
        )
        if rows:
            resource_name = (rows[0].get("geoTargetConstant") or {}).get(
                "resourceName", ""
            )
            if resource_name:
                return resource_name
    except Exception:
        logger.warning(
            "geo_target_constant lookup failed country_code=%s; India fallback",
            country_code,
        )
    return keyword_planner.INDIA_GEO_TARGET


async def country_from_mapped(mapped: list) -> str:
    """Country code for the campaign's resolved areas — recovered by geocoding when the
    location picker didn't capture it, so a UI-picked location resolves AU/GB, not IN."""
    for m in mapped:
        if not isinstance(m, dict):
            continue
        name = m.get("google_name") or m.get("name") or ""
        if not name:
            continue
        try:
            geo = await google_maps_client.geocode(name.replace(",", ", "))
        except Exception:
            geo = None
        if geo and geo.get("country_code"):
            return str(geo["country_code"])
    return ""


async def resolve_geo(
    session_ctx: dict,
    customer_id: str,
    login_customer_id: str,
    context: dict,
) -> dict:
    """Country-level geo for keyword research (city volumes read too low to show a user).

    Country comes from the confirmed location, or — when the picker didn't capture it — from
    the geo the campaign actually resolved to, so it's the real country, not the India fallback.
    """
    product = session_ctx.get("product_data") or {}
    country_code = str(
        (session_ctx.get("_location_meta") or {}).get("country_code") or ""
    ).strip()

    if not country_code:
        mapped = (
            product.get("google_mapped_locations")
            or product.get("target_areas")
            or []
        )
        if mapped:
            country_code = await country_from_mapped(mapped)

    geo_constant = session_ctx.get("_country_geo_target") or ""
    if not geo_constant:
        geo_constant = await resolve_country_geo_target(
            country_code, customer_id, login_customer_id, context
        )
        # Cache only a genuine result: a non-India constant, or India when the country
        # really is IN (else INDIA_GEO_TARGET is just the failure fallback, not the answer).
        if (
            geo_constant
            and country_code
            and (
                geo_constant != keyword_planner.INDIA_GEO_TARGET
                or country_code.upper() == "IN"
            )
        ):
            session_ctx["_country_geo_target"] = geo_constant
    return {
        "geo_target_constants": [geo_constant],
        "hl": "en",
        "gl": country_code or "IN",
        "language": keyword_planner.DEFAULT_LANGUAGE,
    }


def taxonomy_key(product: dict) -> str:
    """Offering fingerprint — re-derive the taxonomy only when the product changes."""
    return f"{product.get('product_name', '')}|{product.get('business_type', '')}"


def location_text(product: dict) -> str:
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
    primary = location_text(product) or city or (names[0] if names else "")
    service_areas = [n for n in names if n and n.lower() != primary.lower()][:8]
    return primary, service_areas


def business_profile(product: dict) -> BusinessProfile:
    # category hint = business_type; the agent derives the exact category + siblings.
    # Source-selection flags stay default (web-search coverage) for v1 — Amazon/YouTube
    # are conditional, future. brand_name = product_name.
    siblings = (
        product.get("category_siblings") or product.get("sibling_categories") or ()
    )
    if isinstance(siblings, str):
        siblings = [s.strip() for s in siblings.split(",") if s.strip()]
    return BusinessProfile(
        category=(
            product.get("business_type") or product.get("product_name") or ""
        ).strip(),
        brand_name=(product.get("product_name") or "").strip(),
        location=location_text(product),
        category_siblings=tuple(siblings),
    )


def business_text(product: dict) -> str:
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
