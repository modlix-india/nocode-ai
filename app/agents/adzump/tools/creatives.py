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
from app.agents.adzump.models import OfferState, offer_state
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
            display_error="Competitor ads are available on Meta campaigns.",
        )
    # Stored-ok exception (HLD/LLD §4.5): the fetch is metered but internal and
    # reversible, so a stored ACCEPTED passes - the user's Yes must not expire
    # because a digression moved the "latest message" (the F-bug where a
    # consented fetch died on the way to the analyze step).
    stored_yes = (
        offer_state(spec, "competitor_creatives") is OfferState.ACCEPTED
    )
    if not stored_yes and not wants_competitor_creatives(_last_user_text(context)):
        return ToolResult(
            success=False,
            error=(
                "Consent gate: fetching competitor creatives costs ad-library "
                "credits, so it needs the user's go-ahead - a stored yes to the "
                "offer, or an explicit yes in their latest message. Ask first "
                'via the present_options tool (field "competitor_creatives"): '
                '"Want to see your competitors\' recent ads?" with chips Yes / '
                "No (answers accepted/declined) - then call this tool only "
                "after a clear yes."
            ),
            display_error="Waiting for your go-ahead before fetching competitor ads.",
        )

    competitive = session_ctx.get("competitor_analysis") or {}
    competitors = competitive.get("competitors") or []
    if not competitors:
        return ToolResult(
            success=False,
            error=(
                "No competitors to fetch creatives for. Run analyze_competitors "
                "NOW, then call fetch_competitor_creatives AGAIN in this same "
                "turn - the user's consent is already given and must not be "
                "asked for twice."
            ),
            display_error="Finding your competitors first…",
        )

    force = bool(params.get("force"))
    await emit_progress(context, "Fetching competitor creatives…")

    # Stream each competitor into the panel as it resolves - a 5-competitor
    # fetch takes minutes end-to-end, and the customer should watch ads land
    # one competitor at a time, not stare at a spinner until the last one.
    business = session_ctx.get("product_data") or {}
    comps_by_key: dict[str, list[dict]] = {}  # several entries can share a domain
    for comp in competitors:
        key, _name = ci.competitor_identity(comp)
        if key:
            comps_by_key.setdefault(key, []).append(comp)

    total_creatives = 0
    resolved = 0

    async def _on_resolved(key: str, record) -> None:
        nonlocal total_creatives, resolved
        comps = comps_by_key.get(key)
        if not comps:
            return
        dumped = record.model_dump(by_alias=True)
        for comp in comps:
            comp["creatives"] = dumped["creatives"]
            comp["totalCreatives"] = dumped["totalCreatives"]
            comp["activeCreatives"] = dumped["activeCreatives"]
        total_creatives += dumped["totalCreatives"]
        resolved += 1
        await emit_progress(
            context,
            f"{record.name or key}: {dumped['totalCreatives']} ads "
            f"({resolved}/{len(comps_by_key)} competitors)…",
        )
        await rerender_craft(session_ctx, context, business,
                             spec.get("platform") or "")

    try:
        results = await ci.creatives_for_all(
            competitors, context, force=force, enrich=_essence_enrich(context),
            on_resolved=_on_resolved)
    except Exception as e:
        logger.warning("fetch_competitor_creatives failed: %s: %s",
                       type(e).__name__, str(e)[:200])
        return ToolResult(
            success=False, error=f"Creative fetch failed: {e}",
            display_error="Couldn't fetch competitor ads right now.",
        )

    # The consented fetch ran to completion - the offer is resolved even when it
    # found nothing (zero ads, no usable domains). An explicit marker, not the
    # creative lists: an empty result must not re-open the consent every turn
    # (see campaign_data.competitor_creatives_offer_resolved).
    # NOTE: on the rare failure path above (cancellation / loop bug), earlier
    # _on_resolved side effects survive while this stays unset - acceptable:
    # the refetch is cache-served for the competitors already resolved.
    session_ctx["_competitor_creatives_fetched"] = True

    summary = (
        f"Fetched creatives for {resolved} competitor"
        f"{'s' if resolved != 1 else ''} ({total_creatives} ads total)."
        if resolved else "No creatives found for the current competitors."
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
        "ads - a stored yes to the offer, or a yes in their latest message "
        '(offer it via present_options, field "competitor_creatives", answers '
        "accepted/declined) - NOT as a routine step of competitor "
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
