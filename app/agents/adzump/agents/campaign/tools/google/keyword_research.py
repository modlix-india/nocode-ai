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

from app.agents.adzump.platform import to_enum_value as platform_enum_value
from app.agents.adzump.services.business_storage import resolve_url

from app.agents.adzump.agents.campaign.craft import emit_campaign_craft
from app.agents.adzump.agents.keyword.agent import get_keyword_research_agent
from app.agents.adzump.agents.keyword.context_helpers import (
    business_profile as _business_profile,
    business_text as _business_text,
    resolve_geo as _resolve_geo,
    resolve_location as _resolve_location,
    taxonomy_key as _taxonomy_key,
)
from app.agents.adzump.agents.keyword.models import KeywordResearchResult
from app.agents.adzump.agents.keyword.taxonomy import derive_offering_taxonomy

logger = logging.getLogger(__name__)

_SUPPORTED_PLATFORM = "google"
_VALID_TYPES = ("brand", "generic")
_RESEARCH_TIMEOUT_SECONDS = 300
_LOG_TRUNCATE = 200


def _research_key(
    customer_id: str, product: dict, types: list[str], geo_constant: str
) -> str:
    """Fingerprint of the inputs a run depends on — used to skip a redundant re-run."""
    return "|".join(
        [
            customer_id,
            str(product.get("product_name", "")),
            str(product.get("business_type", "")),
            geo_constant,
            ",".join(sorted(types)),
        ]
    )


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

    login_customer_id = str(spec.get("parent_account") or "").strip()

    # Resolve country-level geo target before the idempotency key so a location
    # change triggers a fresh research run.
    geo = await _resolve_geo(session_ctx, customer_id, login_customer_id, context)

    # Idempotency: same inputs already researched -> re-show, don't re-run.
    geo_constants = geo.get("geo_target_constants") or [""]
    research_key = _research_key(customer_id, product, types, geo_constants[0])
    cached = session_ctx.get("keyword_research")
    craft_id = (
        session_ctx.get("craft_id") or f"campaign_{context.get('session_id', '')}"
    )
    if cached and (cached.get("meta") or {}).get("key") == research_key:
        await emit_campaign_craft(context.get("event_stream"), craft_id, session_ctx)
        return ToolResult(
            success=True,
            summary="Keywords already researched for these details — showing the saved set.",
            data={"craft_id": craft_id},
        )

    # Offering taxonomy (core vs sibling + local-vs-national) — product analysis doesn't
    # persist one, so derive it from confirmed product_data; cached by offering fingerprint
    # (re-derives only when the product changes), tokens tracked.
    tax_key = _taxonomy_key(product)
    cache = session_ctx.get("_offering_taxonomy") or {}
    if cache.get("key") == tax_key:
        taxonomy = cache["data"]
    else:
        tax_obj, usage = await derive_offering_taxonomy(product)
        taxonomy = tax_obj.model_dump()
        session_ctx["_offering_taxonomy"] = {"key": tax_key, "data": taxonomy}
        session = context.get("_session")
        if session is not None and usage:
            session.accumulate_usage(usage)

    loc_text, service_areas = _resolve_location(
        product, taxonomy.get("is_location_specific", True)
    )
    profile = _business_profile(product)
    agent = get_keyword_research_agent()
    common = dict(
        ad_account={
            "customer_id": customer_id,
            "login_customer_id": login_customer_id,
        },
        geo=geo,
        craft_id=craft_id,
        parent_event_stream=context.get("event_stream"),
        auth=context["auth"],
        category=taxonomy.get("primary_offering") or profile.category,
        core_terms=taxonomy.get("core_terms") or [],
        siblings=taxonomy.get("sibling_categories") or [],
        sources=profile.source_names(),
        location=loc_text,
        service_areas=service_areas,
        business_url=resolve_url(session_ctx) or "",
        business_text=_business_text(product),
    )

    # Brand & generic independent — one failing/timing-out still returns the other.
    results = await asyncio.gather(
        *(
            asyncio.wait_for(
                agent.research(keyword_type=t, **common), _RESEARCH_TIMEOUT_SECONDS
            )
            for t in types
        ),
        return_exceptions=True,
    )

    # meta: geo for the add-keyword endpoint, key for the idempotency guard.
    bundle = KeywordResearchResult(
        meta={
            "craft_id": craft_id,
            "channel": channel,
            "geo": geo,
            "key": research_key,
        }
    )
    counts: list[str] = []
    failed: list[str] = []
    for t, res in zip(types, results):
        if isinstance(res, Exception):
            logger.warning(
                "keyword_research %s failed: %s", t, str(res)[:_LOG_TRUNCATE]
            )
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
    await emit_campaign_craft(context.get("event_stream"), craft_id, session_ctx)
    note = (
        f" Note: {', '.join(failed)} could not be researched this time."
        if failed
        else ""
    )
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
