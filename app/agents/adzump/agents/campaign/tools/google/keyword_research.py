"""keyword_research — research brand + generic keywords for a Google Search campaign.

A campaign-creation step (runs before launch). Reads the assembled campaign_spec +
product_data from session.context, derives a BusinessProfile, then runs the
KeywordResearchAgent for brand and generic in parallel — both stream their
positives/negatives (with volumes) into the review panel under one craft_id.

Google Search only for now; other platforms and campaign types become sibling tools.
"""

from __future__ import annotations

import asyncio
import logging

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

from app.agents.adzump.adapters.google import keyword_planner
from app.agents.adzump.platform import to_enum_value as platform_enum_value
from app.agents.adzump.services.business_storage import resolve_url

from app.agents.adzump.agents.keyword.agent import get_keyword_research_agent
from app.agents.adzump.agents.keyword.models import BusinessProfile, KeywordResearchResult

logger = logging.getLogger(__name__)

_SUPPORTED_PLATFORM = "google"
_VALID_TYPES = ("brand", "generic")
# Geo defaults until the location service lands (India — same fallback as the Planner).
_DEFAULT_GEO = {
    "geo_target_constants": [keyword_planner.INDIA_GEO_TARGET],
    "hl": "en",
    "gl": "IN",
    "language": keyword_planner.DEFAULT_LANGUAGE,
}
_REVIEW_EVENT = "keyword_review"
# Generic panel shape (tabs -> sections -> rows) — reusable for the optimization flow.
_POSITIVE_COLUMNS = ["keyword", "volume", "match_type", "intent"]
_NEGATIVE_COLUMNS = ["keyword", "volume", "match_type", "reason"]
_PANEL_ACTIONS = ["add", "edit", "delete"]
# Per-type hang-catch only (real bound is max_turns + per-call timeouts + breaker).
_RESEARCH_TIMEOUT_SECONDS = 300
_LOG_TRUNCATE = 200


def _research_key(customer_id: str, product: dict, types: list[str]) -> str:
    """Fingerprint of the inputs a run depends on — used to skip a redundant re-run."""
    return "|".join([
        customer_id,
        str(product.get("product_name", "")),
        str(product.get("business_type", "")),
        ",".join(sorted(types)),
    ])


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


def _business_profile(product: dict) -> BusinessProfile:
    # category hint = business_type; the agent derives the exact category + siblings.
    # Source-selection flags stay default (web-search coverage) for v1 — Amazon/YouTube
    # are conditional, future. brand_name = product_name.
    siblings = product.get("category_siblings") or product.get("sibling_categories") or ()
    if isinstance(siblings, str):
        siblings = [s.strip() for s in siblings.split(",") if s.strip()]
    return BusinessProfile(
        category=(
            product.get("business_type") or product.get("product_name") or ""
        ).strip(),
        brand_name=(product.get("product_name") or "").strip(),
        location=_location_text(product),
        category_siblings=tuple(siblings),
    )


def _business_text(product: dict) -> str:
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


async def _emit_review_panel(context: dict, craft_id: str, dump: dict) -> None:
    """Emit ONE consolidated review panel (Brand/Generic tabs x Positives/Negatives).
    Best-effort — a stream error never fails the tool."""
    stream = context.get("event_stream")
    if stream is None:
        return
    tabs = []
    for key, label in (("brand", "Brand"), ("generic", "Generic")):
        kset = dump.get(key)
        if not kset:
            continue
        tabs.append({
            "key": key,
            "label": label,
            "sections": [
                {"key": "positives", "label": "Positives", "columns": _POSITIVE_COLUMNS,
                 "rows": kset.get("positives", []), "actions": _PANEL_ACTIONS},
                {"key": "negatives", "label": "Negatives", "columns": _NEGATIVE_COLUMNS,
                 "rows": kset.get("negatives", []), "actions": _PANEL_ACTIONS},
            ],
        })
    try:
        await stream.emit_data(_REVIEW_EVENT, {
            "craft_id": craft_id, "title": "Keyword Suggestions", "tabs": tabs,
        })
    except Exception as exc:  # observability only
        logger.debug("keyword_review emit failed: %s", str(exc)[:_LOG_TRUNCATE])


async def _keyword_research(params: dict, context: dict) -> ToolResult:
    session_ctx = context.get("session_context")
    if session_ctx is None:
        return ToolResult(success=False, error="No session context available.")
    spec = session_ctx.get("campaign_spec") or {}
    product = session_ctx.get("product_data") or {}

    if platform_enum_value(spec.get("platform")) != _SUPPORTED_PLATFORM:
        return ToolResult(
            success=False,
            error="Keyword research is available for Google Search campaigns only.",
        )

    # Channel gate — Search uses keywords; PMax/others don't (future campaign types).
    channel = str(params.get("channel") or spec.get("channel") or "SEARCH").upper()
    if channel != "SEARCH":
        return ToolResult(
            success=True,
            summary=f"{channel} campaigns don't use keywords — skipped.",
            data={"skipped": True, "channel": channel},
        )

    customer_id = str(spec.get("account") or "").strip()
    if not customer_id:
        return ToolResult(
            success=False,
            error="No ad account selected — set the campaign account first.",
        )
    if context.get("auth") is None:
        return ToolResult(success=False, error="No auth context for keyword research.")

    keyword_type = str(params.get("keyword_type", "both")).lower()
    types = list(_VALID_TYPES) if keyword_type == "both" else [keyword_type]
    if any(t not in _VALID_TYPES for t in types):
        return ToolResult(
            success=False, error="keyword_type must be brand, generic, or both."
        )

    # Idempotency: same inputs already researched -> re-show, don't re-run.
    research_key = _research_key(customer_id, product, types)
    cached = session_ctx.get("keyword_research")
    if cached and (cached.get("meta") or {}).get("key") == research_key:
        cached_craft = (cached.get("meta") or {}).get("craft_id", "")
        await _emit_review_panel(context, cached_craft, cached)
        return ToolResult(
            success=True,
            summary="Keywords already researched for these details — showing the saved set.",
            data={"craft_id": cached_craft},
        )

    profile = _business_profile(product)
    craft_id = (
        session_ctx.get("craft_id") or f"keywords_{context.get('session_id', '')}"
    )
    agent = get_keyword_research_agent()
    common = dict(
        ad_account={
            "customer_id": customer_id,
            "login_customer_id": str(spec.get("parent_account") or "").strip(),
        },
        geo=dict(_DEFAULT_GEO),
        craft_id=craft_id,
        parent_event_stream=context.get("event_stream"),
        auth=context["auth"],
        category=profile.category,
        siblings=list(profile.category_siblings),
        sources=profile.source_names(),
        location=profile.location,
        business_url=resolve_url(session_ctx) or "",
        business_text=_business_text(product),
    )

    # Brand & generic independent — one failing/timing-out still returns the other.
    results = await asyncio.gather(
        *(
            asyncio.wait_for(agent.research(keyword_type=t, **common), _RESEARCH_TIMEOUT_SECONDS)
            for t in types
        ),
        return_exceptions=True,
    )

    # meta: geo for the add-keyword endpoint, key for the idempotency guard.
    bundle = KeywordResearchResult(meta={
        "craft_id": craft_id, "channel": channel, "geo": dict(_DEFAULT_GEO), "key": research_key,
    })
    counts: list[str] = []
    failed: list[str] = []
    for t, res in zip(types, results):
        if isinstance(res, Exception):
            logger.warning("keyword_research %s failed: %s", t, str(res)[:_LOG_TRUNCATE])
            failed.append(t)
            continue
        setattr(bundle, t, res)
        counts.append(f"{t} {len(res.positives)}+/{len(res.negatives)}-")
    if failed:
        bundle.meta["failed"] = failed
    dump = bundle.model_dump(mode="json")
    session_ctx["keyword_research"] = dump

    if not counts:
        return ToolResult(
            success=False,
            error="Keyword research produced no results — check the ad account and retry.",
        )
    await _emit_review_panel(context, craft_id, dump)
    note = f" Note: {', '.join(failed)} could not be researched this time." if failed else ""
    return ToolResult(
        success=True,
        summary="Keywords ready for review — "
        + ", ".join(counts)
        + " (positives/negatives)."
        + note,
        data={"craft_id": craft_id},
    )


keyword_research = ToolDefinition(
    name="keyword_research",
    description=(
        "Research brand + generic keywords for a Google Search campaign. Reads the "
        "campaign account and business from session.context, runs both types in "
        "parallel, and shows positives + negatives (with volumes) in the review panel."
    ),
    display_name="Keyword Research",
    parameters=[
        ToolParameter(
            name="keyword_type",
            type="string",
            required=False,
            description="brand, generic, or both (default both).",
            enum=["brand", "generic", "both"],
        ),
    ],
    execute=_keyword_research,
)

GOOGLE_CAMPAIGN_TOOLS = [keyword_research]
