"""Tool: fetch competitor ad creatives from the Creative Intelligence library.

Operates on the competitors already discovered by ``analyze_competitors`` (in
``session_context['competitor_analysis']``). For each, it reads the shared
library - serving fresh records as-is and fetching from the source only on a miss
or stale entry (see ``creative_intelligence.library``). Discovered creatives are
attached back onto each competitor entry and rendered into the craft panel.

Intended for the creative-inspiration phase - the model calls it when the user
wants to see competitor ads, and later the creative agent will call it to gather
reference. It is deliberately NOT run during routine competitor analysis, so we
don't spend ad-library credits unless creatives are actually wanted.
"""

from __future__ import annotations

import logging

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump._shared import emit_progress
from app.agents.adzump import creative_intelligence as ci
from app.agents.adzump.platform import is_meta
from app.agents.adzump.tools.campaign_data import (
    _last_user_text,
    wants_competitor_creatives,
)
from app.agents.adzump.tools.craft import rerender_craft

logger = logging.getLogger(__name__)


def _essence_enrich(context: dict):
    """The injected Tier-3 hook (see ``creative_intelligence/enrich.py``): one
    Essence Analyst card + one single-shot extract per competitor ingest.
    Constructed HERE - the tool owns orchestration; the library only awaits the
    typed Protocol and never imports the agent."""
    from app.agents.adzump.agents.creative_essence import get_essence_analyst
    from app.core.streaming import pre_emit_agent_started

    stream = context.get("event_stream")
    session_ctx = context.get("session_context", {}) or {}

    async def _enrich(images):
        # The launcher owns the card open; extract() emits agent_finished.
        await pre_emit_agent_started(
            stream, agent_id="creative_essence", label="Essence Analyst",
            parent_tool_use_id=context.get("tool_use_id", ""), context=session_ctx,
        )
        return await get_essence_analyst().extract(
            images, stream, context.get("auth"),
            parent_session_context={
                "url": session_ctx.get("primary_url") or session_ctx.get("url", ""),
                "craft_id": session_ctx.get("craft_id", ""),
            },
        )

    return _enrich


async def _fetch_competitor_creatives(params: dict, context: dict) -> ToolResult:
    """Fetch + cache competitor creatives for the current competitor set.

    Two HARD gates before any spend (ad-library credits + vision tokens), same
    backstop philosophy as launch_campaign - the prompt persuades, the code
    enforces: (1) Meta flow only; (2) the user's LATEST message must be a clear
    go-ahead."""
    session_ctx = context.get("session_context", {}) or {}
    spec = session_ctx.get("campaign_spec") or {}
    if not is_meta(spec.get("platform")):
        return ToolResult(
            success=False,
            error=(
                "Competitor creatives are part of the META flow only (they seed "
                "Meta's creative-bound delivery). This campaign is not on Meta - "
                "do not offer or fetch them."
            ),
        )
    if not wants_competitor_creatives(_last_user_text(context)):
        return ToolResult(
            success=False,
            error=(
                "Consent gate: fetching competitor creatives costs ad-library "
                "credits, so it needs an explicit go-ahead in the user's LATEST "
                "message. Ask first via the present_options tool (field "
                '"competitor_creatives_declined"): "Want to see the ads your '
                'competitors are running?" with chips Yes / No - then call this '
                "tool only after a clear yes."
            ),
        )

    competitive = session_ctx.get("competitor_analysis") or {}
    competitors = competitive.get("competitors") or []
    if not competitors:
        return ToolResult(
            success=False,
            error="No competitors to fetch creatives for. Run analyze_competitors first.",
        )

    force = bool(params.get("force"))
    await emit_progress(context, "Fetching competitor creatives…")

    try:
        results = await ci.creatives_for_all(
            competitors, context, force=force, enrich=_essence_enrich(context))
    except Exception as e:
        logger.warning("fetch_competitor_creatives failed: %s: %s",
                       type(e).__name__, str(e)[:200])
        return ToolResult(success=False, error=f"Creative fetch failed: {e}")

    # The consented fetch ran to completion - the offer is resolved even when it
    # found nothing (zero ads, no usable domains). An explicit marker, not the
    # creative lists: an empty result must not re-open the consent every turn
    # (see campaign_data.competitor_creatives_offer_resolved).
    session_ctx["_competitor_creatives_fetched"] = True

    # Attach creatives back to each competitor entry (persisted in session),
    # then rebuild the panel ONCE - each competitor card nests its own
    # creatives, so there is no appendable standalone section anymore.
    business = session_ctx.get("product_data") or {}

    total_creatives = 0
    enriched = 0
    for comp in competitors:
        key, _name = ci.competitor_identity(comp)
        record = results.get(key)
        if not record:
            continue
        dumped = record.model_dump(by_alias=True)
        comp["creatives"] = dumped["creatives"]
        comp["totalCreatives"] = dumped["totalCreatives"]
        comp["activeCreatives"] = dumped["activeCreatives"]
        total_creatives += dumped["totalCreatives"]
        enriched += 1
    if enriched:
        await rerender_craft(session_ctx, context, business,
                             spec.get("platform") or "")

    summary = (
        f"Fetched creatives for {enriched} competitor"
        f"{'s' if enriched != 1 else ''} ({total_creatives} ads total)."
        if enriched else "No creatives found for the current competitors."
    )
    return ToolResult(
        success=True,
        data={"resolved": list(results.keys()), "total_creatives": total_creatives},
        summary=summary,
        audience="both",
    )


fetch_competitor_creatives = ToolDefinition(
    name="fetch_competitor_creatives",
    description=(
        "Fetch competitor ad creatives (image/video thumbnails, ad copy, metrics, "
        "extracted essence) to use as creative inspiration. META flow only, and "
        "gated on consent: call ONLY after the user says yes to seeing competitor "
        "ads in their latest message (offer it via present_options, field "
        '"competitor_creatives_declined") - NOT as a routine step of competitor '
        "analysis; the tool refuses otherwise. Requires competitors to already "
        "exist (from analyze_competitors). Reuses a shared creative library and "
        "only queries the ad library for competitors that are missing or stale. "
        "Set force=true to ignore the cache and refetch."
    ),
    display_name="Fetch Competitor Creatives",
    parameters=[
        ToolParameter(
            name="force",
            type="boolean",
            description="Set true to refetch from the ad library, ignoring cached library data.",
            required=False,
        ),
    ],
    execute=_fetch_competitor_creatives,
)

CREATIVE_TOOLS = [fetch_competitor_creatives]
