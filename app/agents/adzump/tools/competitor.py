"""Competitor analysis tool - discover and profile competitors.

Spawns the Product Analyst sub-agent with competitor-focused instructions,
cleans the results (filter self-references, bad URLs, aggregators), and
renders a rich craft panel in the UI.
"""

from __future__ import annotations

import asyncio
import logging

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.models import CompetitorProfile
from app.agents.adzump.tools.campaign_data import (
    clear_competitor_decline,
    pending_creatives_fetch_steer,
)
from app.agents.adzump._shared import (
    emit_progress,
    host_of,
    is_aggregator_host,
    primary_screenshot_url,
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


def _vetted_fallback(entry: dict) -> str | None:
    """The join's fallback URL (the fetched page) re-checked against the same
    vetting the options passed - a hallucinated pick or old-shape output must
    never ship a host the options gathering refused (broker-style TLD,
    aggregator). Link-less beats a refused host."""
    from app.agents.adzump.competitor_urls import (
        is_aggregator_or_google_host,
        is_broker_style_tld,
    )

    url = entry.get("fetch_url") or entry.get("url")
    host = host_of(url or "")
    if not url or not host or is_aggregator_or_google_host(host) \
            or is_broker_style_tld(host):
        return None
    return url


def _join_verified_urls(competitive: dict, session_ctx: dict) -> None:
    """B2: the analyst cites fetch_candidates evidence by competitor_id and
    picks the entry's official URL by official_url_id (an id from that
    entry's code-vetted url_options) - both resolve here by ID join, so the
    model never writes URLs. The analyst OWNS the official-URL judgment
    (Kailash: the researcher with the full context judges, not a post-hoc
    model): a null official_url_id means honestly link-less; an unknown pick
    falls back to the fetched page rather than shipping nothing."""
    verified = (session_ctx.get("_research_state") or {}).get(
        "verified_competitors") or []
    by_cid = {v["cid"]: v for v in verified
              if isinstance(v, dict) and v.get("cid")}
    for comp in competitive.get("competitors") or []:
        if not isinstance(comp, dict):
            continue
        cid = comp.pop("competitor_id", None)
        judged = "official_url_id" in comp  # absent != a deliberate null
        uid = comp.pop("official_url_id", None)
        if not cid:
            continue
        entry = by_cid.get(cid)
        if entry is None:
            logger.warning("competitor_id_unknown: %r for %r (valid: %s)",
                           cid, comp.get("name"), sorted(by_cid) or "none")
            continue
        options = entry.get("url_options") or {}
        if not judged:
            # Old-shape output (no pick emitted): the fetched page, as before.
            comp["url"] = _vetted_fallback(entry)
        elif uid is None:
            # The analyst judged: no option is this project's own page.
            comp["url"] = None
        elif uid in options:
            comp["url"] = options[uid]
        else:
            logger.warning("official_url_id_unknown: %r for %r (valid: %s) - "
                           "falling back to the fetched page",
                           uid, comp.get("name"), sorted(options) or "none")
            comp["url"] = _vetted_fallback(entry)


_URL_VERIFY_QUESTION = (
    "FIRST line: write 'MATCH: YES' if this page is the official website or a "
    "dedicated official page for '{name}', or 'MATCH: NO' if it is something "
    "else (a different project, a broker/lead-gen page, a directory, a parked "
    "domain).\n"
    "SECOND line: one short sentence saying what this page actually is."
)


async def _verify_competitor_url(name: str, url: str) -> tuple[str, str]:
    """Verify a user-provided competitor URL before it becomes the entry's
    identity (it keys the shared creative library): reachable, not a
    portal/platform, and the page is actually about {name}. Returns
    (verified_url, "") on success or ("", reason) - the reason is user-facing."""
    from app.agents.adzump.agents.product.adapters.web_fetch_adapter import (
        fetch_and_answer,
    )

    url = (url or "").strip()
    if not host_of(url):
        return "", f"'{url}' is not a valid website URL."
    if _is_bad_url(url):
        return "", (
            f"{host_of(url)} is a portal/platform, not a competitor's own "
            "site - share the project's official website instead."
        )
    try:
        result = await asyncio.wait_for(
            fetch_and_answer(url, _URL_VERIFY_QUESTION.format(name=name)),
            timeout=25.0,
        )
    except Exception:
        result = None
    if not isinstance(result, dict) or result.get("status") != "ok":
        return "", f"Couldn't reach {url} - the site didn't respond."
    final_url = result.get("url") or url
    if _is_bad_url(final_url):
        return "", f"{url} redirects to a portal ({host_of(final_url)}) - not an official site."
    answer = (result.get("answer") or "").strip()
    if answer.upper().startswith("MATCH: YES"):
        return final_url, ""
    # The USER is the authority on their own market: a live, non-portal page
    # they explicitly pinned is ACCEPTED even when the page read disagrees
    # (live 2026-09-08: Nambiar's real project page mentions a channel partner
    # and the reader vetoed the user's correct pin - then the entry stayed
    # link-less and its ads unfetchable). The doubt ships as a caution in the
    # ack; only invalid/dead/portal URLs still reject.
    page_is = answer.split("\n", 1)[1].strip() if "\n" in answer else ""
    return final_url, (
        f"note: the page reads as {page_is} - kept your URL anyway; say the "
        "word if it's wrong" if page_is else ""
    )


def _same_project(name_a: str, name_b: str) -> bool:
    """'Nambiar Villas' and 'Nambiar Bannerghatta Villas' are ONE project
    (compact containment, or same brand token + one name's tokens a subset of
    the other's); 'Purva Sparkling Springs' and 'Purva Sound of Water' are
    TWO (same brand, disjoint project tokens - sibling projects always keep
    separate entries)."""
    from app.agents.adzump.competitor_urls import normalize_business_name

    a = normalize_business_name(name_a)
    b = normalize_business_name(name_b)
    if not a or not b:
        return False
    a_compact, b_compact = a.replace(" ", ""), b.replace(" ", "")
    if a_compact in b_compact or b_compact in a_compact:
        return True
    a_tokens, b_tokens = a.split(), b.split()
    return a_tokens[0] == b_tokens[0] and (
        set(a_tokens) <= set(b_tokens) or set(b_tokens) <= set(a_tokens)
    )


def _refresh_entry(existing: dict, fresh: dict) -> None:
    """Fold a re-looked-up competitor into its existing entry: fill empty
    fields, adopt a newly resolved URL (never over a user pin), and reset the
    creative triad only when the identity host actually changed (the library
    keys on it)."""
    for field in ("business_type", "location", "pricing", "key_usps",
                  "weakness", "why_competitor"):
        if fresh.get(field) and not existing.get(field):
            existing[field] = fresh[field]
    if existing.get("url_source") == "user":
        return
    fresh_url = fresh.get("url")
    if fresh_url and fresh_url != existing.get("url"):
        if host_of(fresh_url) != host_of(existing.get("url") or ""):
            for stale in ("creatives", "totalCreatives", "activeCreatives"):
                existing.pop(stale, None)
        existing["url"] = fresh_url


def _find_competitor(competitive: dict, name: str) -> dict | None:
    """Fuzzy entry lookup for user-referenced names (containment either way -
    'Purva' finds 'Purva Sparkling Springs')."""
    target = _normalize_name(name)
    if not target:
        return None
    for comp in competitive.get("competitors") or []:
        if isinstance(comp, dict):
            comp_norm = _normalize_name(comp.get("name") or "")
            if comp_norm and (target in comp_norm or comp_norm in target):
                return comp
    return None


async def _apply_url_updates(
    set_url: str, competitive: dict,
) -> tuple[list[str], list[str]]:
    """Pin user-provided URLs onto entries: 'Name | URL' (';'-separated for
    several). A verified URL becomes the entry's identity (url_source=user -
    nothing runs after a pin) and the entry's creatives reset so the next
    fetch runs under the corrected identity. Returns (acks, rejections), both
    user-facing."""
    acks: list[str] = []
    rejections: list[str] = []
    for spec in (set_url or "").split(";"):
        spec = spec.strip()
        if not spec:
            continue
        if "|" not in spec:
            rejections.append(f"Couldn't parse '{spec}' - expected 'Name | URL'.")
            continue
        name, url = (part.strip() for part in spec.split("|", 1))
        entry = _find_competitor(competitive, name)
        if entry is None:
            rejections.append(
                f"No competitor named '{name}' in the list - add it first "
                "(query), then set its URL."
            )
            continue
        entry_name = entry.get("name") or name
        verified_url, note = await _verify_competitor_url(entry_name, url)
        if not verified_url:
            rejections.append(note)
            continue
        entry["url"] = verified_url
        entry["url_source"] = "user"
        for stale in ("creatives", "totalCreatives", "activeCreatives"):
            entry.pop(stale, None)
        logger.info("competitor_url_user_set: %r -> %s%s", entry_name,
                    verified_url, f" ({note})" if note else "")
        acks.append(f"{entry_name}: website updated to {verified_url}"
                    + (f" ({note})" if note else ""))
    return acks, rejections


def _normalize_entries(competitive: dict) -> None:
    """Round-trip every entry through CompetitorProfile so the stored shape is
    the model's contract (folds the legacy product_name key into name)."""
    comps = competitive.get("competitors")
    if not isinstance(comps, list):
        return
    competitive["competitors"] = [
        CompetitorProfile.from_stored(c).to_stored()
        for c in comps
        if isinstance(c, dict)
    ]


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
    business_url_host = host_of(raw_url)
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
        if business_url_host and url and host_of(url) == business_url_host:
            dropped += 1
            continue
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


# ── Craft panel rendering - delegated to tools/craft.py ──────────────
from app.agents.adzump.tools.craft import (
    emit_craft_panel as _emit_final_craft,
    append_competitor_blocks as _append_competitor_craft,
)


# ── Tool implementation ───────────────────────────────────────────────


async def _analyze_competitors(params: dict, context: dict) -> ToolResult:
    """Re-entrancy shield: the model batches several analyze_competitors calls
    in one turn (live 2026-09-08: two IN PARALLEL) - concurrent analysts race
    the shared research state and each paints its own panel group. One at a
    time; the duplicate call gets a calm refusal, not a second analyst."""
    session_ctx = context.get("session_context", {}) or {}
    if session_ctx.get("_competitor_analysis_running"):
        return ToolResult(
            success=False,
            error=(
                "analyze_competitors is ALREADY running - never call it more "
                "than once per turn (one call handles every name; pass names "
                "comma-separated in `query`). Use the running call's result."
            ),
            display_error="Competitor research is already in progress…",
        )
    session_ctx["_competitor_analysis_running"] = True
    try:
        return await _analyze_competitors_impl(params, context)
    finally:
        session_ctx.pop("_competitor_analysis_running", None)


async def _analyze_competitors_impl(params: dict, context: dict) -> ToolResult:
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

    # Focused add/remove/URL-pin by name.
    query = (params.get("query") or "").strip()
    remove = (params.get("remove") or "").strip()
    set_url = (params.get("set_url") or "").strip()
    # The model sometimes passes the SUBJECT product's own name as the query on
    # the fetch->"analyze first"->retry path; looking that up just skips it as
    # "same business" and discovers nothing. Drop any query name matching the
    # subject so a subject-only query falls through to real discovery.
    if query:
        subject = _normalize_name(product_name)
        query = ", ".join(
            n.strip() for n in query.split(",")
            if n.strip() and _normalize_name(n) != subject
        )
    if query or remove or set_url:
        if auth is None:
            return ToolResult(success=False, error="Authentication required.")
        return await _lookup_single_competitor(
            query,
            remove,
            set_url,
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
            summary = f"Already analyzed: {comp_count} competitors found."
            return ToolResult(
                success=True,
                data={"competitive": existing},
                summary=summary,
                model_summary=summary + pending_creatives_fetch_steer(context),
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
                        summary = f"Reused {comp_count} competitors from storage."
                        return ToolResult(
                            success=True,
                            data={"competitive": existing, "from_storage": True},
                            summary=summary,
                            model_summary=summary + pending_creatives_fetch_steer(context),
                            audience="both",
                        )
        except Exception as e:
            logger.warning(
                "competitor_storage_hydrate_skipped: %s: %s",
                type(e).__name__,
                str(e)[:200],
            )
    else:
        # force = re-run the research fresh (clear the pipeline scratch), but
        # NEVER throw away the existing entries - their creatives and user
        # pins survive, and fresh results MERGE in below (live 2026-09-08:
        # force-discovery wiped the list and every re-run painted another
        # 'Competitors' group onto the panel).
        session_ctx.get("_research_state", {}).clear()

    if auth is None:
        return ToolResult(success=False, error="Authentication required.")

    try:
        from app.agents.adzump.agents.product.agent import get_product_agent

        await emit_progress(context, "Starting competitor research…")
        # Symmetric lifecycle: the launcher owns both AgentCard ends -
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
            enforce_verified_competitors=True,
            user_message=(
                f"Run competitor research for: {product_name}\n\n"
                f"Product profile:\n{product_summary[:1500]}\n\n"
                "SCOPE: Do NOT call scrape_url - the business is already analyzed.\n"
                "1) Derive 7 search queries from the profile:\n"
                "   - Queries 1-5: direct discovery (offering_type, geography, price_tier, "
                "adjacent format, customer overlap). At least 2 spec-anchored to specific "
                "product variants.\n"
                '   - Queries 6-7: review/comparison (e.g. "best {offering} in {geography} '
                'reviews", "{offering} market report", "{offering} vs alternatives"). '
                "These target expert/media content for authority signal.\n"
                "2) Issue SEVEN `web_search` calls - one per query. The server runs each "
                "search and returns results inline.\n"
                "3) After ALL searches complete, call `extract_candidates()`, judge every "
                "row yourself (one-line PICK/SKIP verdict each, per your system prompt's "
                "Step 4 rules), then call `fetch_candidates` with the 6-8 strongest IDs.\n"
                "4) FINAL MESSAGE must be a SINGLE ```json fenced block per the schema "
                "in your system prompt. Include ONLY direct head-to-head competitors "
                "per your own Step 5 judgment, grounded in the fetch_candidates "
                "evidence - do not re-add anything that failed to verify. "
                "No prose outside the JSON."
            ),
        )

        if not output.competitive:
            raise RuntimeError("Agent produced no usable JSON")

        competitive = output.competitive
        _normalize_entries(competitive)
        _join_verified_urls(competitive, session_ctx)

        # Post-processing: clean aggregator URLs, filter self-references.
        # URLs are settled: the analyst judged each entry's official page from
        # the code-vetted options (official_url_id, joined above); nothing
        # re-judges URLs after this point.
        _clean_urls(competitive)
        _filter_self_references(business, competitive, primary_url=url)

        # Merge into the existing list, never replace it: a re-discovery
        # ("find more competitors") refreshes known entries in place (pins and
        # creatives survive) and appends only the genuinely new ones - ONE
        # competitor group on the panel, however many times research runs.
        fresh_competitors = competitive.get("competitors") or []
        existing_analysis = session_ctx.get("competitor_analysis") or {}
        had_existing = bool(existing_analysis.get("competitors"))
        appended: list[dict] = fresh_competitors
        refreshed_count = 0
        if had_existing:
            _normalize_entries(existing_analysis)
            existing_list: list[dict] = existing_analysis["competitors"]
            appended = []
            for fresh in fresh_competitors:
                match = next(
                    (c for c in existing_list if isinstance(c, dict)
                     and _same_project(c.get("name") or "",
                                       fresh.get("name") or "")),
                    None,
                )
                if match is None:
                    existing_list.append(fresh)
                    appended.append(fresh)
                else:
                    _refresh_entry(match, fresh)
                    refreshed_count += 1
            competitive = existing_analysis

        session_ctx["competitor_analysis"] = competitive
        # F26 - fresh analysis ran (even if 0 found): a prior decline is void.
        if clear_competitor_decline(session_ctx):
            logger.info("competitor_decline_cleared: analyze_competitors ran")

        craft_id = session_ctx.get("craft_id", "")
        if stream and craft_id:
            if had_existing:
                # Entries may have changed in place - repaint the whole panel
                # (appending again would paint a second 'Competitors' group).
                await _emit_final_craft(
                    stream,
                    craft_id,
                    url,
                    business,
                    competitive,
                    screenshot_url=primary_screenshot_url(business),
                    baked_summary=product_summary,
                )
            else:
                # First batch - append with the "Competitors" heading.
                await _append_competitor_craft(
                    stream,
                    craft_id,
                    business,
                    appended,
                    include_headers=True,
                )

        competitors = competitive.get("competitors") or []
        comp_count = len(competitors)
        names = [c.get("name", "?") for c in (appended or competitors)[:5]]

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

        if had_existing:
            summary = (
                f"Research complete: {len(appended)} new "
                f"({', '.join(names) or 'none'}), {refreshed_count} already "
                f"known and refreshed - {comp_count} competitors total."
            )
        else:
            summary = f"Found {comp_count} competitors: {', '.join(names)}"
        return ToolResult(
            success=True,
            data={"competitive": competitive},
            summary=summary,
            model_summary=summary + pending_creatives_fetch_steer(context),
            audience="both",
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
    set_url: str,
    product_name: str,
    product_summary: str,
    primary_url: str,
    stream,
    tool_use_id: str,
    auth,
    session_ctx: dict,
    context: dict,
) -> ToolResult:
    """Add, remove, and/or pin URLs for specific competitors by name.

    - `remove`: comma-separated names to drop from competitor_analysis.
    - `query`: comma-separated names to look up via ProductAgent and add.
    - `set_url`: user-provided 'Name | URL' pins, verified then applied
      (runs after additions so add-with-URL works in one call).
    After changes, the craft panel is rebuilt in full if any removals or URL
    pins happened, or appended to if only additions.
    """
    import time as _time

    _run_start = _time.monotonic()

    competitive = session_ctx.setdefault("competitor_analysis", {"competitors": []})
    competitive.setdefault("competitors", [])
    _normalize_entries(competitive)  # heals legacy product_name entries in place
    competitors_list: list[dict] = competitive["competitors"]
    skipped: list[dict] = []

    # ── Removals ──
    removed_names: list[str] = []
    removal_notes: list[str] = []
    if remove:
        # Traceability guard: removal names must be the USER's words. Live
        # 2026-09-09: 'remove sobha magnum' was relayed as remove='Sobha
        # Magnus' - the model conflated near-identical siblings and deleted
        # the REAL project while the phantom stayed. A relayed name absent
        # from the user's message is swapped for the entry the user actually
        # named (when that's unambiguous), else skipped with a note.
        from app.agents.adzump.tools.campaign_data import _last_user_text
        user_compact = _normalize_name(_last_user_text(context))
        names_to_remove: set[str] = set()
        for requested in (n.strip() for n in remove.split(",") if n.strip()):
            requested_norm = _normalize_name(requested)
            if not user_compact or requested_norm in user_compact:
                names_to_remove.add(requested_norm)
                continue
            user_named = [
                c for c in competitors_list
                if isinstance(c, dict)
                and _normalize_name(c.get("name") or "")
                and _normalize_name(c.get("name") or "") in user_compact
            ]
            if len(user_named) == 1:
                actual = user_named[0].get("name") or "?"
                logger.warning("competitor_remove_swapped: tool said %r, the "
                               "user named %r - removing the user's pick",
                               requested, actual)
                names_to_remove.add(_normalize_name(actual))
            else:
                removal_notes.append(
                    f"Didn't remove '{requested}' - the user's message names "
                    "a different (or no single) entry; confirm which one."
                )
        kept: list[dict] = []
        for c in competitors_list:
            cname = _normalize_name(c.get("name") or "")
            if cname in names_to_remove:
                removed_names.append(c.get("name") or "?")
            else:
                kept.append(c)
        competitive["competitors"] = kept
        competitors_list = kept

    # ── Additions via ProductAgent ──
    new_competitors: list[dict] = []
    refreshed_names: list[str] = []
    if query:
        from app.agents.adzump.agents.product.agent import get_product_agent

        await emit_progress(context, f"Looking up {query}…")
        # Symmetric lifecycle: the launcher owns both AgentCard ends -
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
            enforce_verified_competitors=True,
            user_message=(
                f"The USER named these businesses as their competitors: {query}\n\n"
                f"Our product: {product_name} - {product_summary[:500]}\n\n"
                "THE COMPETITOR DECISION IS ALREADY MADE - the advertiser "
                "knows their market (an apartment CAN compete with a villa: "
                "same wallet, upsell within a price margin). Your job is to "
                "VERIFY and PROFILE each named business, never to gatekeep "
                "it.\n\n"
                "Search each name with web_search (1-2 focused queries per "
                "name), then run the SAME verification pipeline as full "
                "discovery: extract_candidates(), judge the rows, "
                "fetch_candidates with the picks. Do NOT call scrape_url.\n\n"
                "For EACH queried business:\n"
                " - ADD it (with its verified profile) - even when its format, "
                "price tier, or corridor differs from ours; note the "
                "difference in why_competitor instead of skipping.\n"
                " - SKIP ONLY when you cannot find the business at all, or "
                "the name is too ambiguous to identify one business (reason: "
                "'not found' / 'ambiguous - which X did you mean?'). NEVER "
                "skip for format/price/location.\n"
                " - TYPO RESOLUTION: when a named business does not verifiably "
                "exist but the evidence clearly points to a near-identical "
                "REAL project ('Sobha Magnum' when only 'Sobha Magnus' "
                "exists), add THE REAL project under its correct name and "
                "state the correction in why_competitor. NEVER build an entry "
                "for a phantom name out of clone/prelaunch sites - broker "
                "clones squat on every plausible spelling.\n\n"
                "Return a ```json block with:\n"
                "- 'competitive.competitors' array: one entry per ADDED business "
                "with name, competitor_id (its fetch_candidates ID), "
                "official_url_id (ONE of that entry's Official-URL options, or "
                "null), url: null, business_type, location, pricing, key_usps, "
                "weakness, why_competitor.\n"
                "- 'competitive.skipped' array: one entry per SKIPPED business with "
                "{name, reason} - reason is ≤15 words (e.g. 'different area', "
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
            # Same custody join as discovery: competitor_id -> verified entry,
            # official_url_id -> the analyst's URL pick from the vetted options.
            _join_verified_urls(output.competitive, session_ctx)
            raw_new = output.competitive["competitors"]
        elif output.product:
            # Business-shaped dict (product_name, not name); from_stored folds
            # it. Its url is model-typed with no verified evidence - ship the
            # entry link-less (custody: a model string never becomes a URL).
            output.product.pop("url", None)
            raw_new = [output.product]
        else:
            raw_new = []
        new_competitors = [
            CompetitorProfile.from_stored(c).to_stored()
            for c in raw_new
            if isinstance(c, dict)
        ]

        skipped = (output.competitive or {}).get("skipped") or []


        # A looked-up name that matches an existing entry is a REFRESH, never
        # a duplicate (live 2026-09-08: "check Nambiar's official website"
        # appended a second Nambiar card). The existing entry updates in
        # place; sibling projects (disjoint project tokens) stay separate.
        truly_new: list[dict] = []
        for fresh in new_competitors:
            existing = next(
                (c for c in competitors_list if isinstance(c, dict)
                 and _same_project(c.get("name") or "", fresh.get("name") or "")),
                None,
            )
            if existing is None:
                truly_new.append(fresh)
                continue
            _refresh_entry(existing, fresh)
            refreshed_names.append(
                f"{existing.get('name') or '?'}"
                + (f" ({existing.get('url')})" if existing.get("url")
                   else " (no official website found)")
            )
        new_competitors = truly_new
        competitors_list.extend(new_competitors)
        # F26 - competitors were ADDED by name → a prior decline is void. (Not on
        # a pure removal: zeroing the list isn't a reversal of the decline.)
        if new_competitors and clear_competitor_decline(session_ctx):
            logger.info("competitor_decline_cleared: competitors added by name")

    # ── User-provided URL pins (after additions, so add-with-URL works) ──
    url_acks: list[str] = []
    url_rejections: list[str] = []
    if set_url:
        url_acks, url_rejections = await _apply_url_updates(set_url, competitive)

    # ── Nothing happened ──
    if not removed_names and not new_competitors and not skipped \
            and not url_acks and not refreshed_names and not removal_notes:
        if url_rejections:
            return ToolResult(
                success=False,
                error="URL not updated: " + " ".join(url_rejections)
                + " Relay this to the user.",
                display_error=url_rejections[0],
            )
        return ToolResult(
            success=False,
            error=f"Could not find information about '{query}'. Ask the user for a URL.",
        )

    # ── Craft panel update ──
    business = session_ctx.get("product_data") or {}
    craft_id = session_ctx.get("craft_id", "")
    if stream and craft_id:
        if removed_names or url_acks or refreshed_names:
            # Full rebuild - append=False replaces the panel entirely (a URL
            # pin or an in-place refresh changes an existing card's link;
            # appending can't fix that).
            await _emit_final_craft(
                stream,
                craft_id,
                primary_url,
                business,
                competitive,
                screenshot_url=primary_screenshot_url(business),
                baked_summary=product_summary,
            )
        elif new_competitors:
            await _append_competitor_craft(stream, craft_id, business, new_competitors)

    # The summary is USER-FACING chat markdown (audience=both): structured
    # bullets, never a period-joined paragraph (live 2026-09-09 complaint).
    sections: list[str] = []
    if removed_names:
        sections.append("**Removed:** " + ", ".join(removed_names))
    if removal_notes:
        sections.append("**Not removed:**\n"
                        + "\n".join(f"- {n}" for n in removal_notes))
    if new_competitors:
        names = [c.get("name") or "?" for c in new_competitors]
        sections.append("**Added:**\n" + "\n".join(f"- {n}" for n in names))
    if refreshed_names:
        sections.append("**Refreshed (already in the list):**\n"
                        + "\n".join(f"- {r}" for r in refreshed_names))
    if url_acks:
        sections.append("**Updated:**\n" + "\n".join(f"- {a}" for a in url_acks))
    if url_rejections:
        sections.append("**Not updated:**\n"
                        + "\n".join(f"- {r}" for r in url_rejections))
    if skipped:
        skip_lines = [
            f"- {s.get('name', '?')} - {s.get('reason', 'not a direct competitor')}"
            for s in skipped
            if isinstance(s, dict)
        ]
        if skip_lines:
            sections.append("**Skipped:**\n" + "\n".join(skip_lines))
    return ToolResult(
        success=True,
        data={"competitors": competitive["competitors"], "skipped": skipped},
        summary="\n\n".join(sections),
        audience="both",
    )


# ── Tool definition ───────────────────────────────────────────────────

analyze_competitors = ToolDefinition(
    name="analyze_competitors",
    description=(
        "Competitive analysis via the Product Analyst agent. Four modes: "
        "(1) No params: full competitor discovery (7 web searches + candidate judging). "
        "(2) query=names: look up specific competitors by name - also the way "
        "to re-check an existing competitor's website (a matching name "
        "refreshes that entry in place, never duplicates it). "
        "(3) remove=names: drop competitors the user rejected. "
        "(4) set_url: when the USER provides or corrects a competitor's "
        "website, verify and pin it (never edit a URL any other way - there "
        "is no other way). Modes combine in one call: user says 'add X, their "
        "site is Y' -> query='X' + set_url='X | Y'."
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
        ToolParameter(
            name="set_url",
            type="string",
            description=(
                "USER-provided website for a competitor, format 'Name | https://url' "
                "(';'-separated for several). The URL is verified (reachable, not a "
                "portal, page is actually about that competitor) before it is pinned; "
                "on success the entry's creatives reset so the next fetch uses the "
                "corrected site, on failure the result carries a rejection reason to "
                "relay to the user. Only for URLs the user stated - never guess one."
            ),
            required=False,
        ),
    ],
    execute=_analyze_competitors,
)

COMPETITOR_TOOLS = [analyze_competitors]
