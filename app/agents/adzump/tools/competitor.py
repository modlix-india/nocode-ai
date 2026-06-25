"""Competitor analysis tool — discover and profile competitors.

Spawns the Product Analyst sub-agent with competitor-focused instructions,
cleans the results (filter self-references, bad URLs, aggregators), and
renders a rich craft panel in the UI.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.tools.campaign_data import clear_competitor_decline
from app.agents.adzump._shared import (
    emit_progress,
    host_of,
    is_aggregator_host,
)

logger = logging.getLogger(__name__)


# ── Competitor URL / name cleaning ────────────────────────────────────

# Content platforms that aren't competitor homepages but aren't in the
# general AGGREGATOR_HOSTS set (which tracks portals/social/marketplaces).
_CONTENT_PLATFORM_HOSTS: frozenset[str] = frozenset(
    {
        "tiktok.com",
        "medium.com",
        "substack.com",
    }
)


def _is_bad_url(url: str) -> bool:
    host = host_of(url)
    if not host:
        return False
    return is_aggregator_host(host, _CONTENT_PLATFORM_HOSTS)


def _normalize_name(s: str) -> str:
    """Lowercase + strip non-alphanumerics for name-similarity comparisons."""
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _clean_urls(competitive: dict) -> int:
    """Strip competitor URLs that point to forums/aggregators/social.
    Returns the count of URLs scrubbed."""
    scrubbed = 0
    comps = competitive.get("competitors")
    if not isinstance(comps, list):
        return 0
    for c in comps:
        if isinstance(c, dict) and _is_bad_url(str(c.get("url") or "")):
            c["url"] = None
            scrubbed += 1
    return scrubbed


def _filter_self_references(
    business: dict,
    competitive: dict,
    primary_url: str = "",
) -> int:
    """Drop obviously-invalid competitor entries (self-references, non-
    competitor platforms, missing name). Returns count removed."""
    comps = competitive.get("competitors")
    if not isinstance(comps, list):
        return 0

    business_name_norm = _normalize_name(business.get("product_name") or "")
    raw_url = primary_url or business.get("url") or business.get("website") or ""
    business_url_host = ""
    try:
        business_url_host = urlparse(raw_url).netloc.lower().removeprefix("www.")
    except Exception:
        pass
    domain_name_norm = (
        _normalize_name(business_url_host.split(".")[0]) if business_url_host else ""
    )

    NON_COMPETITOR_HINTS = (
        "investment platform",
        "aggregator",
        "marketplace",
        "comparison site",
        "review site",
        "directory",
        "property portal",
        "real estate intelligence",
    )

    kept: list[dict] = []
    dropped = 0
    for c in comps:
        if not isinstance(c, dict):
            kept.append(c)
            continue
        name = (c.get("name") or "").strip()
        if not name:
            dropped += 1
            continue
        lower = name.lower()
        if "(self)" in lower or lower.startswith("self"):
            dropped += 1
            continue
        name_norm = _normalize_name(name)
        if business_name_norm and name_norm == business_name_norm:
            dropped += 1
            continue
        if (
            domain_name_norm
            and len(domain_name_norm) > 3
            and domain_name_norm in name_norm
        ):
            dropped += 1
            continue
        url = str(c.get("url") or "")
        if business_url_host and url:
            try:
                host = urlparse(url).netloc.lower().removeprefix("www.")
                if host == business_url_host:
                    dropped += 1
                    continue
            except Exception:
                pass
        combined_text = " ".join(
            [
                str(c.get("business_type") or ""),
                str(c.get("weakness") or ""),
            ]
        ).lower()
        if any(hint in combined_text for hint in NON_COMPETITOR_HINTS):
            dropped += 1
            continue
        kept.append(c)

    if dropped:
        competitive["competitors"] = kept
    return dropped


# ── Craft panel rendering — delegated to tools/craft.py ──────────────
from app.agents.adzump.tools.craft import (
    emit_craft_panel as _emit_final_craft,
    append_competitor_blocks as _append_competitor_craft,
    render_competitors as _render_competitors,
)


# ── Tool implementation ───────────────────────────────────────────────


async def _analyze_competitors(params: dict, context: dict) -> ToolResult:
    """Spawn the Product Analyst agent to do competitor research.

    Thin bridge: business check → cache check → spawn agent → clean → persist → render craft.
    """
    import time as _time

    _run_start = _time.monotonic()

    stream = context.get("event_stream")
    tool_use_id = context.get("tool_use_id", "")
    auth = context.get("auth")
    session_ctx = context.get("session_context", {}) or {}

    business = session_ctx.get("product_data")
    if not business:
        brief = session_ctx.get("product_profile")
        if brief and brief.get("summary"):
            logger.info("analyze_competitors: using product_profile as fallback")
            business = {
                "product_name": brief.get("title", ""),
                "summary": brief.get("summary", ""),
            }
            session_ctx["product_data"] = business
        else:
            return ToolResult(
                success=False,
                error="No product profile found. Call analyze_product first.",
            )

    brief = session_ctx.get("product_profile", {})
    product_summary = brief.get("summary", "") or business.get("summary", "")
    product_name = business.get("product_name", "the business")
    url = brief.get("url", "")
    if not url:
        url = (business.get("pages_analyzed") or [None])[0] or ""

    # Focused add/remove by name.
    query = (params.get("query") or "").strip()
    remove = (params.get("remove") or "").strip()
    if query or remove:
        if auth is None:
            return ToolResult(success=False, error="Authentication required.")
        return await _lookup_single_competitor(
            query,
            remove,
            product_name,
            product_summary,
            url,
            stream,
            tool_use_id,
            auth,
            session_ctx,
            context,
        )

    # Return cached competitor results unless force-refresh requested.
    force = str(params.get("force", "")).lower() in ("true", "1", "yes")
    if not force:
        existing = session_ctx.get("competitor_analysis")
        if existing and existing.get("competitors"):
            comp_count = len(existing["competitors"])
            return ToolResult(
                success=True,
                data={"competitive": existing},
                summary=f"Already analyzed: {comp_count} competitors found.",
                audience="both",
            )
        # Cross-session: try the storage record before spawning the sub-agent.
        try:
            from app.agents.adzump.services.business_storage import hydrate_from_storage

            if url:
                hit = await hydrate_from_storage(url, session_ctx, context)
                if hit:
                    existing = session_ctx.get("competitor_analysis")
                    if existing and existing.get("competitors"):
                        comp_count = len(existing["competitors"])
                        return ToolResult(
                            success=True,
                            data={"competitive": existing, "from_storage": True},
                            summary=f"Reused {comp_count} competitors from storage.",
                            audience="both",
                        )
        except Exception as e:
            logger.warning(
                "competitor_storage_hydrate_skipped: %s: %s",
                type(e).__name__,
                str(e)[:200],
            )
    else:
        # Clear stale results so the pipeline runs fresh.
        session_ctx.pop("competitor_analysis", None)
        session_ctx.get("research_state", {}).clear()

    if auth is None:
        return ToolResult(success=False, error="Authentication required.")

    try:
        from app.agents.adzump.agents.product.agent import get_product_agent

        await emit_progress(context, "Starting competitor research…")
        # Symmetric lifecycle: the launcher owns both AgentCard ends —
        # agent_started here, agent_finished after post-processing.
        from app.core.streaming import pre_emit_agent_started

        await pre_emit_agent_started(
            stream,
            agent_id="product_analyst",
            label="Product Analyst",
            parent_tool_use_id=tool_use_id,
            context=context,
        )
        output = await get_product_agent().analyze(
            url=url,
            parent_event_stream=stream,
            parent_tool_use_id=tool_use_id,
            auth=auth,
            parent_session_context=session_ctx,
            user_message=(
                f"Run competitor research for: {product_name}\n\n"
                f"Product profile:\n{product_summary[:1500]}\n\n"
                "SCOPE: Do NOT call scrape_url — the business is already analyzed.\n"
                "1) Derive 7 search queries from the profile:\n"
                "   - Queries 1-5: direct discovery (offering_type, geography, price_tier, "
                "adjacent format, customer overlap). At least 2 spec-anchored to specific "
                "product variants.\n"
                '   - Queries 6-7: review/comparison (e.g. "best {offering} in {geography} '
                'reviews", "{offering} market report", "{offering} vs alternatives"). '
                "These target expert/media content for authority signal.\n"
                "2) Issue SEVEN `web_search` calls — one per query. The server runs each "
                "search and returns results inline.\n"
                "3) After ALL searches complete, call `shortlist_competitors()` in the SAME "
                "response. It scores, classifies, filters aggregators, fetches the top 6-8 "
                "in parallel, drops fetch failures, and returns a verified evidence block.\n"
                "4) FINAL MESSAGE must be a SINGLE ```json fenced block per the schema "
                "in your system prompt. Include ONLY direct head-to-head competitors — "
                "skip anything the shortlist marked ADJACENT or ALTERNATIVE. Transcribe "
                "from the shortlist evidence; do not re-add anything it dropped. "
                "No prose outside the JSON."
            ),
        )

        if not output.competitive:
            raise RuntimeError("Agent produced no usable JSON")

        competitive = output.competitive

        # Post-processing: clean aggregator URLs, filter self-references.
        _clean_urls(competitive)
        _filter_self_references(business, competitive, primary_url=url)

        session_ctx["competitor_analysis"] = competitive
        # F26 — fresh analysis ran (even if 0 found): a prior decline is void.
        if clear_competitor_decline(session_ctx):
            logger.info("competitor_decline_cleared: analyze_competitors ran")

        # Append competitor blocks to the existing craft panel. This is the
        # first batch, so show the "Competitors" heading.
        craft_id = session_ctx.get("craft_id", "")
        if stream and craft_id:
            await _append_competitor_craft(
                stream,
                craft_id,
                business,
                competitive.get("competitors") or [],
                include_headers=True,
            )

        competitors = competitive.get("competitors") or []
        comp_count = len(competitors)
        names = [c.get("name", "?") for c in competitors[:5]]

        duration_ms = int((_time.monotonic() - _run_start) * 1000)
        if stream:
            try:
                await stream.emit_agent_finished(
                    agent_id="product_analyst",
                    status="success",
                    duration_ms=duration_ms,
                    summary=f"Found {comp_count} competitor{'s' if comp_count != 1 else ''}",
                )
            except Exception:
                pass

        summary = f"Found {comp_count} competitors: {', '.join(names)}"
        return ToolResult(
            success=True, data={"competitive": competitive}, summary=summary, audience="both"
        )

    except Exception as e:
        logger.warning(
            "analyze_competitors failed: %s: %s", type(e).__name__, str(e)[:200]
        )
        duration_ms = int((_time.monotonic() - _run_start) * 1000)
        if stream:
            try:
                await stream.emit_agent_finished(
                    agent_id="product_analyst",
                    status="error",
                    duration_ms=duration_ms,
                    summary=str(e)[:100],
                )
            except Exception:
                pass
        return ToolResult(success=False, error=f"Competitor research failed: {e}")


async def _lookup_single_competitor(
    query: str,
    remove: str,
    product_name: str,
    product_summary: str,
    primary_url: str,
    stream,
    tool_use_id: str,
    auth,
    session_ctx: dict,
    context: dict,
) -> ToolResult:
    """Add and/or remove specific competitors by name.

    - `remove`: comma-separated names to drop from competitor_analysis.
    - `query`: comma-separated names to look up via ProductAgent and add.
    After changes, the craft panel is rebuilt in full if any removals happened,
    or appended to if only additions.
    """
    import time as _time

    _run_start = _time.monotonic()

    competitive = session_ctx.setdefault("competitor_analysis", {"competitors": []})
    competitors_list: list[dict] = competitive.setdefault("competitors", [])
    skipped: list[dict] = []

    # ── Removals ──
    removed_names: list[str] = []
    if remove:
        names_to_remove = {_normalize(n) for n in remove.split(",") if n.strip()}
        kept: list[dict] = []
        for c in competitors_list:
            cname = _normalize(c.get("name") or c.get("product_name") or "")
            if cname in names_to_remove:
                removed_names.append(c.get("name") or c.get("product_name") or "?")
            else:
                kept.append(c)
        competitive["competitors"] = kept
        competitors_list = kept

    # ── Additions via ProductAgent ──
    new_competitors: list[dict] = []
    if query:
        from app.agents.adzump.agents.product.agent import get_product_agent

        await emit_progress(context, f"Looking up {query}…")
        # Symmetric lifecycle: the launcher owns both AgentCard ends —
        # agent_started here, agent_finished after post-processing.
        from app.core.streaming import pre_emit_agent_started

        await pre_emit_agent_started(
            stream,
            agent_id="product_analyst",
            label="Product Analyst",
            parent_tool_use_id=tool_use_id,
            context=context,
        )
        output = await get_product_agent().analyze(
            url="",
            parent_event_stream=stream,
            parent_tool_use_id=tool_use_id,
            auth=auth,
            parent_session_context=session_ctx,
            user_message=(
                f"Look up these businesses as potential direct competitors: {query}\n\n"
                f"Our product: {product_name} — {product_summary[:500]}\n\n"
                "Check existing research data in context first; for any not found, "
                "use web_search. Do NOT call scrape_url.\n\n"
                "For EACH queried business, decide:\n"
                " - ADD if it's a true head-to-head competitor (same offering type, "
                "same geography, similar price tier).\n"
                " - SKIP if it's only adjacent/alternative or you couldn't find info.\n\n"
                "Return a ```json block with:\n"
                "- 'competitive.competitors' array: one entry per ADDED business with "
                "name, url, business_type, location, pricing, key_usps, weakness, "
                "why_competitor.\n"
                "- 'competitive.skipped' array: one entry per SKIPPED business with "
                "{name, reason} — reason is ≤15 words (e.g. 'different area', "
                "'different price tier', 'not found on web').\n"
                "- Empty 'business' section.\n"
                "Every queried name must appear in exactly one of the two arrays."
            ),
        )

        duration_ms = int((_time.monotonic() - _run_start) * 1000)
        if stream:
            try:
                await stream.emit_agent_finished(
                    agent_id="product_analyst",
                    status="success" if output.competitive else "error",
                    duration_ms=duration_ms,
                    summary=f"Looked up {query}",
                )
            except Exception:
                pass

        if output.competitive and output.competitive.get("competitors"):
            new_competitors = output.competitive["competitors"]
        elif output.product:
            new_competitors = [output.product]

        skipped = (output.competitive or {}).get("skipped") or []

        competitors_list.extend(new_competitors)
        # F26 — competitors were ADDED by name → a prior decline is void. (Not on
        # a pure removal: zeroing the list isn't a reversal of the decline.)
        if new_competitors and clear_competitor_decline(session_ctx):
            logger.info("competitor_decline_cleared: competitors added by name")

    # ── Nothing happened ──
    if not removed_names and not new_competitors and not skipped:
        return ToolResult(
            success=False,
            error=f"Could not find information about '{query}'. Ask the user for a URL.",
        )

    # ── Craft panel update ──
    business = session_ctx.get("product_data") or {}
    craft_id = session_ctx.get("craft_id", "")
    if stream and craft_id:
        if removed_names:
            # Full rebuild — append=False replaces the panel entirely.
            await _emit_final_craft(
                stream,
                craft_id,
                primary_url,
                business,
                competitive,
                screenshot_url=(business.get("primary_screenshot_url") or business.get("screenshot_url")),
                baked_summary=product_summary,
            )
        elif new_competitors:
            await _append_competitor_craft(stream, craft_id, business, new_competitors)

    parts: list[str] = []
    if removed_names:
        parts.append(f"Removed: {', '.join(removed_names)}")
    if new_competitors:
        names = [c.get("product_name") or c.get("name") or "?" for c in new_competitors]
        parts.append(f"Added: {', '.join(names)}")
    if skipped:
        skip_lines = [
            f"{s.get('name', '?')} ({s.get('reason', 'not a direct competitor')})"
            for s in skipped
            if isinstance(s, dict)
        ]
        if skip_lines:
            parts.append(f"Skipped: {'; '.join(skip_lines)}")
    return ToolResult(
        success=True,
        data={"competitors": competitive["competitors"], "skipped": skipped},
        summary=". ".join(parts),
        audience="both",
    )


def _normalize(name: str) -> str:
    """Lowercase + strip non-alphanumerics for name comparison."""
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


# ── Tool definition ───────────────────────────────────────────────────

analyze_competitors = ToolDefinition(
    name="analyze_competitors",
    description=(
        "Competitive analysis via the Product Analyst agent. Three modes: "
        "(1) No params: full competitor discovery (7 web searches + shortlist). "
        "(2) query=names: look up specific competitors by name. "
        "(3) remove=names: drop competitors the user rejected. "
        "query and remove can be combined in a single call when the user both "
        "adds and discards competitors in the same message."
    ),
    display_name="Analyze Competitors",
    parameters=[
        ToolParameter(
            name="force",
            type="string",
            description="Set to 'true' to re-run with fresh searches, ignoring cached results.",
            required=False,
        ),
        ToolParameter(
            name="query",
            type="string",
            description="Competitor name(s) to look up. Pass ALL names in ONE call, comma-separated (e.g. 'Urban Paradise, SNN Estates, Lodha Azur'). Do NOT call this tool multiple times for different names.",
            required=False,
        ),
        ToolParameter(
            name="remove",
            type="string",
            description="Competitor name(s) to remove from the list, comma-separated (e.g. 'Birla Trimaya, Some Other'). Use when the user says a competitor isn't relevant. Can be combined with query in the same call.",
            required=False,
        ),
    ],
    execute=_analyze_competitors,
)

COMPETITOR_TOOLS = [analyze_competitors]
