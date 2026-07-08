"""research_competitor_keywords — research brand-name keywords for each analyzed competitor.

A top-level tool the main agent calls directly (like analyze_competitors), not routed
through CampaignAgent — CampaignAgent's loop builds the whole campaign; this only needs
one narrow Keyword-Planner-backed research pass, after brand+generic keyword research
and competitor analysis have both already run. Google Search only, same as keyword_research.

Research + review only: results land in session_ctx["competitor_keywords"] for the
review panel, same as brand/generic — nothing here gets published to the live account.
"""

from __future__ import annotations

import asyncio
import logging

from app.core.tools.base import ToolDefinition, ToolResult

from app.agents.adzump.platform import to_enum_value as platform_enum_value
from app.agents.adzump.services.business_storage import resolve_url

from app.agents.adzump.agents.campaign.craft import emit_campaign_craft
from app.agents.adzump.agents.keyword.agent import get_keyword_research_agent
from app.agents.adzump.agents.keyword.context_helpers import (
    business_profile,
    business_text,
    resolve_geo,
    resolve_location,
)
from app.agents.adzump.agents.keyword.taxonomy import derive_competitor_taxonomies

logger = logging.getLogger(__name__)

_SUPPORTED_PLATFORM = "google"
_RESEARCH_TIMEOUT_SECONDS = 300
_LOG_TRUNCATE = 200


def _research_key(customer_id: str, names: list[str], geo_constant: str) -> str:
    """Fingerprint of the inputs a run depends on — used to skip a redundant re-run."""
    return "|".join([customer_id, geo_constant, ",".join(sorted(names))])


def _merge_taxonomy(competitor: dict, taxonomy) -> dict:
    """Competitor record + its derived taxonomy, in the shape research() expects."""
    return {
        "name": competitor.get("name", ""),
        "rich_summary": competitor.get("rich_summary", ""),
        "primary_offering": taxonomy.primary_offering,
        "core_terms": taxonomy.core_terms,
        "sibling_categories": taxonomy.sibling_categories,
    }


async def _research_competitor_keywords(params: dict, context: dict) -> ToolResult:
    session_ctx = context.get("session_context")
    if session_ctx is None:
        return ToolResult(success=False, error="No session context available.")
    spec = session_ctx.get("campaign_spec") or {}
    product = session_ctx.get("product_data") or {}

    if platform_enum_value(spec.get("platform")) != _SUPPORTED_PLATFORM:
        return ToolResult(
            success=False,
            error="Competitor keyword research is available for Google Search campaigns only.",
        )
    channel = str(spec.get("channel") or "SEARCH").upper()
    if channel != "SEARCH":
        return ToolResult(
            success=True,
            summary=f"{channel} campaigns don't use keywords — skipped.",
            data={"skipped": True, "channel": channel},
        )

    competitors = (session_ctx.get("competitor_analysis") or {}).get("competitors") or []
    # Stripped to match derive_competitor_taxonomies' result keys (str(name).strip()).
    named = [
        {**c, "name": str(c["name"]).strip()}
        for c in competitors
        if str(c.get("name") or "").strip()
    ]
    if not named:
        return ToolResult(
            success=False,
            error="No analyzed competitors found — run competitor analysis first.",
        )
    if not session_ctx.get("keyword_research"):
        return ToolResult(
            success=False,
            error="Run keyword research (brand + generic) before competitor keywords.",
        )

    customer_id = str(spec.get("account") or "").strip()
    if not customer_id:
        return ToolResult(
            success=False,
            error="No ad account selected — set the campaign account first.",
        )
    auth = context.get("auth")
    if auth is None:
        return ToolResult(success=False, error="No auth context for keyword research.")
    login_customer_id = str(spec.get("parent_account") or "").strip()

    geo = await resolve_geo(session_ctx, customer_id, login_customer_id, context)
    geo_constants = geo.get("geo_target_constants") or [""]
    names = [c["name"] for c in named]
    research_key = _research_key(customer_id, names, geo_constants[0])
    craft_id = (
        session_ctx.get("campaign_craft_id")
        or session_ctx.get("craft_id")
        or f"campaign_{context.get('session_id', '')}"
    )
    if session_ctx.get("_competitor_keywords_key") == research_key:
        await emit_campaign_craft(context.get("event_stream"), craft_id, session_ctx)
        return ToolResult(
            success=True,
            summary="Competitor keywords already researched for these details — showing the saved set.",
            data={"craft_id": craft_id},
        )

    taxonomies, usage = await derive_competitor_taxonomies(named)
    session = context.get("_session")
    if session is not None and usage:
        session.accumulate_usage(usage)

    # Location scoping is an ADVERTISER fact (do they serve a specific area, or sell
    # nationally/online?) — read from the advertiser's own already-derived taxonomy
    # (cached by the brand/generic keyword_research call this tool requires to have
    # run first), never from competitor taxonomies. A conquest campaign is still
    # scoped to where the advertiser itself can convert, not any competitor's area.
    own_taxonomy = (session_ctx.get("_offering_taxonomy") or {}).get("data") or {}
    loc_text, service_areas = resolve_location(
        product, own_taxonomy.get("is_location_specific", True)
    )
    profile = business_profile(product)
    research_input = [
        _merge_taxonomy(c, taxonomies[c["name"]]) for c in named if c["name"] in taxonomies
    ]

    agent = get_keyword_research_agent()
    try:
        result = await asyncio.wait_for(
            agent.research(
                keyword_type="competitor_brand",
                ad_account={"customer_id": customer_id, "login_customer_id": login_customer_id},
                geo=geo,
                craft_id=craft_id,
                parent_event_stream=context.get("event_stream"),
                auth=auth,
                sources=profile.source_names(),
                location=loc_text,
                service_areas=service_areas,
                business_url=resolve_url(session_ctx) or "",
                business_text=business_text(product),
                competitors=research_input,
            ),
            _RESEARCH_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning(
            "research_competitor_keywords failed: %s", str(exc)[:_LOG_TRUNCATE]
        )
        return ToolResult(
            success=False,
            error="Competitor keyword research failed — check the ad account and retry.",
        )

    if not isinstance(result, dict) or not result:
        return ToolResult(
            success=False,
            error="Competitor keyword research produced no results — check the ad account and retry.",
        )

    session_ctx["competitor_keywords"] = {
        name: kset.model_dump(mode="json") for name, kset in result.items()
    }
    session_ctx["_competitor_keywords_key"] = research_key
    await emit_campaign_craft(context.get("event_stream"), craft_id, session_ctx)

    counts = [f"{name} {len(kset.positives)}+" for name, kset in result.items()]
    return ToolResult(
        success=True,
        summary="Competitor keywords ready for review — " + ", ".join(counts),
        data={"craft_id": craft_id},
    )


research_competitor_keywords = ToolDefinition(
    name="research_competitor_keywords",
    description=(
        "Research brand-name keyword terms for each analyzed competitor (e.g. "
        "'CompetitorX pricing', 'CompetitorX reviews') for future conquest campaigns. "
        "Covers every analyzed competitor in one pass and shows results as a "
        "Competitors tab in the keyword review panel. Call once, after keyword_research "
        "has completed and the user has opted in. Takes no parameters."
    ),
    display_name="Competitor Keyword Research",
    parameters=[],
    execute=_research_competitor_keywords,
)

COMPETITOR_KEYWORD_TOOLS = [research_competitor_keywords]
