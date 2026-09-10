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
import time

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump._shared import emit_progress
from app.agents.adzump import creative_intelligence as ci
from app.agents.adzump.models import CompetitorProfile, OfferState, offer_state
from app.agents.adzump.platform import is_meta
from app.agents.adzump.tools.campaign_data import (
    _last_user_text,
    wants_competitor_creatives,
)
from app.agents.adzump.tools.craft import rerender_craft

logger = logging.getLogger(__name__)


class _CompetitorSpans:
    """One AgentCard row per competitor in the current fetch. A span opens
    lazily on its first stage message, its status line narrates every phase,
    and it closes with the essence rollup as the row's takeaway. The essence
    worker attributes its progress + insight lines here too - the user sees
    competitors, never agent instances."""

    def __init__(self, context: dict, session_ctx: dict) -> None:
        self._context = context
        self._session_ctx = session_ctx
        self._stream = context.get("event_stream")
        self._parent_tuid = context.get("tool_use_id", "")
        self._open: dict[str, dict] = {}

    async def _ensure(self, key: str, name: str) -> dict:
        span = self._open.get(key)
        if span is None:
            from app.core.streaming import pre_emit_agent_started

            agent_id = f"competitor:{key}"
            tuid = await pre_emit_agent_started(
                self._stream, agent_id=agent_id, label=name or key,
                parent_tool_use_id=self._parent_tuid,
                context=self._session_ctx,
            )
            span = {"tuid": tuid, "agent_id": agent_id,
                    "started": time.monotonic()}
            self._open[key] = span
        return span

    async def stage(self, key: str, name: str, message: str) -> None:
        span = await self._ensure(key, name)
        await emit_progress(self._context, message, tool_use_id=span["tuid"])

    async def ids(self, key: str, name: str) -> tuple[str, str]:
        """(tuid, agent_id) for the essence worker's attribution."""
        span = await self._ensure(key, name)
        return span["tuid"], span["agent_id"]

    async def close(self, key: str, *, ads: int, creatives: list[dict]) -> None:
        span = self._open.pop(key, None)
        if span is None or self._stream is None:
            return
        await self._stream.emit_agent_finished(
            agent_id=span["agent_id"], status="success",
            duration_ms=int((time.monotonic() - span["started"]) * 1000),
            step_count=ads,
            summary=_essence_rollup(ads, creatives),
        )

    async def close_leftovers(self) -> None:
        for key in list(self._open):
            span = self._open.pop(key)
            if self._stream is None:
                continue
            await self._stream.emit_agent_finished(
                agent_id=span["agent_id"], status="error",
                duration_ms=int((time.monotonic() - span["started"]) * 1000),
                summary="did not finish - dropped from this batch",
            )


def _essence_rollup(ads: int, creatives: list[dict]) -> str:
    """The row's takeaway, computed from stored verdicts (never a model call):
    '15 ads · hooks: offer 6, aspiration 3 · 9 video / 6 static'."""
    hooks: dict[str, int] = {}
    video = static = analyzed = 0
    for c in creatives or []:
        essence = c.get("essence") if isinstance(c, dict) else None
        if not essence:
            continue
        analyzed += 1
        hook = essence.get("hookType") or essence.get("hook_type") or ""
        if hook and hook != "other":
            hooks[hook] = hooks.get(hook, 0) + 1
        if (c.get("mediaType") or c.get("media_type")) == "video":
            video += 1
        else:
            static += 1
    parts = [f"{ads} ad{'s' if ads != 1 else ''}"]
    if analyzed:
        top = sorted(hooks.items(), key=lambda kv: -kv[1])[:2]
        if top:
            parts.append("hooks: " + ", ".join(
                f"{h.replace('_', ' ')} {n}" for h, n in top))
        parts.append(f"{video} video / {static} static")
    return " · ".join(parts)


def _essence_enrich(context: dict, spans: _CompetitorSpans):
    """The injected Tier-3 hook (see ``creative_intelligence/enrich.py``): one
    single-shot extract per competitor ingest, narrating onto that
    competitor's card row. Constructed HERE - the tool owns orchestration; the
    library only awaits the typed Protocol and never imports the agent."""
    from app.agents.adzump.agents.creative_essence import get_essence_analyst

    stream = context.get("event_stream")
    session_ctx = context.get("session_context", {}) or {}

    async def _enrich(images, *, key: str = "", name: str = ""):
        tuid, agent_id = await spans.ids(key, name)
        return await get_essence_analyst().extract(
            images, stream, context.get("auth"),
            parent_session_context={
                "url": session_ctx.get("primary_url") or session_ctx.get("url", ""),
                "craft_id": session_ctx.get("craft_id", ""),
            },
            status_tuid=tuid,
            insight_agent_id=agent_id,
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
    # Index-aligned with `competitors` so the write-back lands on the right entry.
    profiles = [
        CompetitorProfile.from_stored(c) if isinstance(c, dict) else None
        for c in competitors
    ]
    # Session-level cache: an entry whose creatives already landed this session
    # is skipped (its craft card is done; the shared store is the CROSS-session
    # cache and must not be the only guard - while it's unavailable a re-run,
    # e.g. adding one more competitor, must not re-spend credits on the rest).
    # force re-fetches everyone.
    fetchable: list = []
    keyed_indices: dict[str, list[int]] = {}  # several entries can share a domain
    for i, profile in enumerate(profiles):
        if profile is None:
            continue
        if not force and profile.creatives is not None:
            continue
        key, _name = ci.competitor_identity(profile)
        if key:
            keyed_indices.setdefault(key, []).append(i)
            fetchable.append(profile)

    if not fetchable:
        session_ctx["_competitor_creatives_fetched"] = True
        return ToolResult(
            success=True,
            data={"competitors": competitors},
            summary="Creatives already fetched for every current competitor - nothing new to fetch.",
            audience="both",
        )

    # One live card row PER COMPETITOR (the user thinks in competitors, not
    # agent instances): its span opens when its search starts, its status line
    # narrates every phase (search / found / saving / reading essence), and it
    # closes with the essence rollup as the takeaway. A minutes-long fetch must
    # never show a silent spinner.
    spans = _CompetitorSpans(context, session_ctx)

    total_creatives = 0
    resolved = 0

    async def _on_resolved(key: str, record) -> None:
        nonlocal total_creatives, resolved
        indices = keyed_indices.get(key)
        if not indices:
            return
        dumped = record.model_dump(by_alias=True)
        for i in indices:
            profile = profiles[i]
            profile.creatives = dumped["creatives"]
            profile.total_creatives = dumped["totalCreatives"]
            profile.active_creatives = dumped["activeCreatives"]
            competitors[i] = profile.to_stored()
        total_creatives += dumped["totalCreatives"]
        resolved += 1
        await spans.close(key, ads=dumped["totalCreatives"],
                          creatives=dumped["creatives"])
        await rerender_craft(session_ctx, context, business,
                             spec.get("platform") or "")

    try:
        results = await ci.creatives_for_all(
            fetchable, context, force=force,
            enrich=_essence_enrich(context, spans), on_resolved=_on_resolved,
            on_stage=spans.stage)
    except Exception as e:
        logger.warning("fetch_competitor_creatives failed: %s: %s",
                       type(e).__name__, str(e)[:200])
        return ToolResult(
            success=False, error=f"Creative fetch failed: {e}",
            display_error="Couldn't fetch competitor ads right now.",
        )
    finally:
        # A competitor that failed/timed out mid-pipeline must not leave a
        # forever-spinning row.
        await spans.close_leftovers()

    # The consented fetch ran to completion - the offer is resolved even when it
    # found nothing (zero ads, no usable domains). An explicit marker, not the
    # creative lists: an empty result must not re-open the consent every turn
    # (see campaign_data.creatives_offer_resolution).
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
