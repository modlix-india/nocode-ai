"""Competitor discovery tools - facts + enforcement in code, judgment in the agent.

CP-6 cutover: two tools replace the old shortlist_competitors composite.
``extract_candidates`` pools/dedupes search hits into an ID'd fact table; the
AGENT (which read the full search content in its own context) judges which
candidates are real competitors; ``fetch_candidates(ids)`` maps the picked IDs
back to custody-held URLs and runs the mechanical pipeline: GBP URL resolution,
host dedup, parallel fetch-verify with aggregator-follow. The Haiku classifier,
composite weights, score threshold, geo hard-floor, and SEGMENT hints are gone -
the worse-informed judge no longer vetoes the better-informed one.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump._shared import (
    emit_progress,
    host_of,
)
from app.agents.adzump.competitor_urls import (
    cached_business_listing,
    is_aggregator_or_google_host,
    listing_name_matches,
    normalize_business_name,
    parse_official_url,
)

logger = logging.getLogger(__name__)

_MAX_FETCH_IDS = 12
_FETCH_TIMEOUT_SEC = 20.0

# Whole-word markers - a token equal to one of these signals a sub-city
# anchor. E.g. "Bannerghatta Road" has the token "road"; "JP Nagar Phase 5"
# has "nagar" AND "phase".
_SPECIFIC_GEO_MARKER_WORDS = frozenset({
    "road", "rd", "street", "st", "avenue", "ave", "boulevard", "blvd",
    "lane", "highway", "hwy",
    "neighborhood", "neighbourhood", "locality", "district",
    "zone", "quadrant", "quarter",
    "nagar", "layout", "colony", "extension", "phase", "sector", "block",
})

# Compound-suffix markers - localities whose name BAKES the marker into the
# word itself (common in Indian cities: "Indiranagar", "Koramangala",
# "JP Nagar" collapsed into "jpnagar", "Malleshpalya"). We match if a token
# ENDS with one of these.
_SPECIFIC_GEO_COMPOUND_SUFFIXES = (
    "nagar", "halli", "pura", "palya", "gudi", "pet", "layout", "colony",
)

_FETCH_QUESTION_TEMPLATE = (
    "FIRST line: write either 'TYPE: BRAND' (this is the brand's own official site "
    "with info about ONE company/product) or 'TYPE: AGGREGATOR' (directory listing "
    "many unrelated brands, comparison portal, marketplace, map search, news article, "
    "or review site).\n"
    "SECOND line (only if AGGREGATOR): write 'OFFICIAL_URL: <url>' with the brand's "
    "own website URL if it is explicitly linked on this page, OR 'OFFICIAL_URL: none' "
    "if no such link is present. Do NOT invent a URL - only use one that appears on the page.\n\n"
    "Then answer: What does this page describe? What does the brand offer, where do "
    "they operate, what's their pricing or business model, and what stands out "
    "(trust signals, differentiators, target customer)?"
)


# ─── Tool 1: extract_candidates - code facts only ───────────────────────────

async def _extract_candidates(params: dict, context: dict) -> ToolResult:
    """Harvest search hits (transcript fallback), dedupe, flag
    aggregator/primary, count cross-query frequency, assign the stable IDs (B2)
    everything downstream keys on. NO judgment: the agent, which read the full
    search content, picks. URLs stay in the session-held candidate pool - the
    model-visible table shows only hosts (the model never transcribes URLs)."""
    session_ctx = context.get("session_context") or {}
    research_state = session_ctx.setdefault("_research_state", {})
    search_results: list[dict] = research_state.get("search_results") or []

    # Anthropic server-side web_search doesn't stash results in session_context
    # (it happens inside the API, not in our Python) - parse them out of this
    # run's message history. History wins over any stashed copy: a rerun in the
    # same session must judge ITS searches, not a previous run's stale stash.
    messages_getter = context.get("session_messages")
    messages = messages_getter() if callable(messages_getter) else (messages_getter or [])
    harvested = _extract_search_results_from_history(messages)
    if harvested:
        search_results = harvested
        research_state["search_results"] = harvested

    if not search_results:
        return ToolResult(
            success=False,
            error="No search results available - run web_search first, then call extract_candidates.",
        )

    business_brief = session_ctx.get("product_profile") or {}
    primary_host = host_of(business_brief.get("url") or "")
    product_name = (session_ctx.get("product_data") or {}).get("product_name", "")

    merged = _merge_candidate_facts(search_results, primary_host, product_name)
    self_references = [c for c in merged if c["is_primary"]]
    candidates = [c for c in merged if not c["is_primary"]]
    if not candidates:
        return ToolResult(
            success=False,
            error="Every search hit resolved to the client's own business - "
                  "broaden the searches or finish with an empty competitor list.",
        )
    for i, cand in enumerate(candidates, start=1):
        cand["cid"] = f"C{i}"
    research_state["candidate_pool"] = {c["cid"]: c for c in candidates}

    n_queries = len(search_results)
    lines = [
        f"## Candidate Facts ({len(candidates)} candidates from {n_queries} searches)",
        "",
        "ID | name | host | seen in | flags",
    ]
    for c in candidates:
        flags = "aggregator-hosted" if c["is_aggregator"] else "-"
        lines.append(
            f"{c['cid']} | {_table_cell(c['name'])} | {c['host'] or '-'} | "
            f"{len(c['seen_in'])}/{n_queries} | {flags}"
        )
    if self_references:
        lines += ["", "Excluded as the client's own business: "
                  + ", ".join(_table_cell(c["name"]) for c in self_references)]
    profile_summary = business_brief.get("summary") or ""
    if _is_specific_geography(profile_summary):
        lines += ["", "Geography flag: this business is anchored to a specific "
                  "micro-market (road/neighborhood level). Apply the "
                  "same-micro-market rule strictly - state a reason for every "
                  "wrong-geography exclusion."]
    lines += ["", "Judge every row against the search content you already read "
              "(one-line PICK/SKIP verdict each), then call fetch_candidates "
              f"with the 6-8 strongest IDs (max {_MAX_FETCH_IDS})."]
    return ToolResult(success=True, summary="\n".join(lines))


# ─── Tool 2: fetch_candidates - code enforcement ────────────────────────────

async def _fetch_candidates(params: dict, context: dict) -> ToolResult:
    """Map the agent's picked IDs to custody-held URLs and run the mechanical
    pipeline: GBP URL fill, host dedup, parallel fetch-verify with
    aggregator-follow. Unknown IDs are an evidence-bearing error; already
    verified IDs are skipped, not re-fetched. Returns ID-keyed evidence -
    a competitor without a verified session entry structurally cannot ship."""
    session_ctx = context.get("session_context") or {}
    research_state = session_ctx.setdefault("_research_state", {})
    pool: dict[str, dict] = research_state.get("candidate_pool") or {}
    if not pool:
        return ToolResult(
            success=False,
            error="No candidate pool - call extract_candidates first.",
        )

    ids: list[str] = []
    for raw in params.get("ids") or []:
        cid = str(raw).strip().upper()
        if cid and cid not in ids:
            ids.append(cid)
    if not ids:
        return ToolResult(success=False, error="Pass the candidate IDs to fetch, e.g. ids=[\"C1\",\"C4\"].")
    unknown = [cid for cid in ids if cid not in pool]
    if unknown:
        return ToolResult(
            success=False,
            error=(f"Unknown candidate IDs: {', '.join(unknown)}. Valid IDs are "
                   f"C1..C{len(pool)} from extract_candidates - re-check your picks."),
        )
    if len(ids) > _MAX_FETCH_IDS:
        return ToolResult(
            success=False,
            error=(f"{len(ids)} IDs is over the fetch budget of {_MAX_FETCH_IDS} - "
                   "pick only the strongest candidates."),
        )

    already_verified = {c.get("cid") for c in research_state.get("verified_competitors") or []}
    skipped_verified = [cid for cid in ids if cid in already_verified]
    picked = [pool[cid] for cid in ids if cid not in already_verified]
    for c in picked:  # pool dicts persist across calls - clear stale markers
        c.pop("dropped_dup_host", None)
        c.pop("no_url", None)

    fetched: list[dict[str, Any]] = []
    if picked:
        # Places URL resolution (D-5/CP-4 scope): missing/aggregator URLs get one
        # GBP lookup; a guard-passing listing wins. A resolved URL is NOT trusted -
        # it joins fetch-verify like any search-derived URL. Nothing is guessed.
        await _resolve_urls(picked, session_ctx)
        picked = _dedupe_resolved_hosts(picked)
        fetchable = [c for c in picked if c.get("url")]
        for c in picked:
            if not c.get("url"):
                c["no_url"] = True  # shadow-table outcome marker
        if fetchable:
            await emit_progress(
                context, f"Fetching {len(fetchable)} competitor pages in parallel…")
            fetched = await asyncio.gather(
                *(_fetch_one_candidate(c) for c in fetchable),
                return_exceptions=False,
            )

    _log_candidate_table(list(pool.values()), set(ids), fetched, already_verified)

    verified = [c for c in fetched if c.get("fetch_status") == "ok"]
    aggregator_drops = [c for c in fetched if c.get("fetch_status") == "aggregator"]
    fetch_fails = [c for c in fetched if c.get("fetch_status") == "failed"]
    logger.info(
        "fetch_candidates: picked=%d skipped_verified=%d fetched=%d verified=%d "
        "(aggregator=%d fetch_fail=%d)",
        len(ids), len(skipped_verified), len(fetched), len(verified),
        len(aggregator_drops), len(fetch_fails),
    )

    # Accumulate across calls (cid-keyed): a second fetch_candidates call with
    # replacement IDs must not erase earlier verified evidence (B2 join source).
    all_verified = {c.get("cid"): c
                    for c in research_state.get("verified_competitors") or []}
    all_verified.update({c["cid"]: c for c in verified})
    research_state["verified_competitors"] = list(all_verified.values())

    lines = _evidence_block(verified, aggregator_drops, fetch_fails,
                            skipped_verified, len(all_verified))
    return ToolResult(
        success=True,
        data={"verified": verified,
              "dropped": {"aggregator": aggregator_drops, "fetch_fail": fetch_fails}},
        summary="\n".join(lines),
    )


def _evidence_block(verified: list[dict], aggregator_drops: list[dict],
                    fetch_fails: list[dict], skipped_verified: list[str],
                    total_verified: int) -> list[str]:
    """ID-keyed evidence for the agent's final judgment - no segment hints,
    no match booleans: the agent re-judges each entry on the fetched content."""
    if not verified and not skipped_verified:
        return [
            "## Fetch Candidates - NOTHING VERIFIED",
            "",
            f"{len(aggregator_drops)} were aggregator pages with no recoverable "
            f"brand site, {len(fetch_fails)} failed to fetch.",
            "",
            "You may call fetch_candidates ONCE more with different IDs, or write "
            "the final JSON citing only competitors you can ground in evidence "
            "(empty competitors array if none, with a note explaining it).",
        ]

    lines: list[str] = [f"## Verified Competitors ({len(verified)} new, "
                        f"{total_verified} total)", ""]
    for c in verified:
        lines.append(f"### {c['name']}")
        lines.append(f"ID: {c['cid']}")
        # The VERIFIED url: fetch_url is the page we actually read (post
        # aggregator-follow/redirects). Cite by ID in the final JSON - code
        # attaches this URL; hand-copied URLs get corrupted.
        verified_url = c.get("fetch_url") or c.get("url")
        if verified_url:
            lines.append(f"URL: {verified_url}")
        if c.get("summary"):
            lines.append(f"Snippet: {c['summary']}")
        answer = (c.get("fetch_answer") or "").strip()
        if answer.upper().startswith("TYPE: BRAND"):
            answer = answer[len("TYPE: BRAND"):].lstrip(":\n ").strip()
        if answer:
            lines.append("")
            lines.append(f"Answer: {answer}")
        lines.append("")

    footer: list[str] = []
    if skipped_verified:
        footer.append(f"Already verified earlier, not re-fetched: "
                      + ", ".join(skipped_verified))
    if fetch_fails:
        footer.append(f"Dropped due to fetch failure ({len(fetch_fails)}): "
                      + ", ".join(c["name"] for c in fetch_fails))
    if aggregator_drops:
        footer.append(f"Dropped as aggregator ({len(aggregator_drops)}): "
                      + ", ".join(c["name"] for c in aggregator_drops))
    if footer:
        lines += ["---", ""] + footer
    return lines


# ─── Candidate facts (stage-1 helpers) ──────────────────────────────────────

def _table_cell(text: str) -> str:
    """Candidate names are raw page titles - a '|' or newline in one would
    shift the fact table's columns and misattribute host/flags to the wrong
    brand. Collapse whitespace, swap the delimiter."""
    return " ".join((text or "").split()).replace("|", "/")


def _merge_candidate_facts(
    search_results: list[dict[str, Any]], primary_host: str,
    primary_name: str = "",
) -> list[dict[str, Any]]:
    """Dedupe search hits into one entry per brand and attach the code facts:
    cross-query frequency (``seen_in``), aggregator flag, self-reference flag.

    Self-reference detection uses host (cityville.in), name fuzzy match
    (``Valmark CityVille`` ≈ ``Valmark City Ville``), AND the client's brand
    token in the candidate host - the developer's own domain (valmark.in for a
    Valmark CityVille campaign) must never enter the competitor list, even
    under an SEO title the name match can't catch. Leading token only, same
    convention as the D-6 brand exclusion: "Godrej Bannerghatta" contributes
    "godrej", never the locality.
    """
    primary_name_norm = normalize_business_name(primary_name)
    primary_brand = (primary_name_norm.split() or [""])[0]
    primary_brand = primary_brand if len(primary_brand) > 3 else ""
    merged: dict[str, dict[str, Any]] = {}

    for search in search_results:
        query = search.get("query", "")
        for cand in search.get("candidates", []):
            if not isinstance(cand, dict):
                continue
            name = str(cand.get("name") or "").strip()
            if not name:
                continue
            url = cand.get("url") or None
            host = host_of(url)
            # Prefer URL host as dedup key (same site = same brand); fall back
            # to normalized name so we still merge when the URL is missing.
            # Use brand name for dedup when URL is an aggregator/citation
            # (e.g. google.com/maps) - all such URLs share the same host,
            # which would incorrectly merge unrelated brands into one entry.
            key = host if (host and not is_aggregator_or_google_host(host)) else normalize_business_name(name)
            if not key:
                continue
            entry = merged.get(key)
            if entry is None:
                entry = {
                    "name": name,
                    "url": url,
                    "host": host,
                    "summary": str(cand.get("summary") or "").strip(),
                    "relevance_note": str(cand.get("relevance_note") or "").strip(),
                    "seen_in": [],
                }
                merged[key] = entry
            else:
                # Fill missing fields from later occurrences.
                if not entry["url"] and url:
                    entry["url"] = url
                    entry["host"] = host or entry["host"]
                if not entry["summary"]:
                    entry["summary"] = str(cand.get("summary") or "").strip()
                if not entry["relevance_note"]:
                    entry["relevance_note"] = str(cand.get("relevance_note") or "").strip()
            if query and query not in entry["seen_in"]:
                entry["seen_in"].append(query)

    out: list[dict[str, Any]] = []
    for entry in merged.values():
        host = entry["host"]
        # Self-reference: match on host OR fuzzy name overlap.
        # Compare with spaces stripped too ("cityville" vs "city ville").
        name_norm = normalize_business_name(entry["name"])
        name_compact = name_norm.replace(" ", "")
        primary_compact = primary_name_norm.replace(" ", "")
        entry["is_primary"] = (
            (bool(primary_host) and host == primary_host)
            or (bool(primary_compact) and len(primary_compact) > 3
                and (primary_compact in name_compact or name_compact in primary_compact))
            or (bool(primary_brand) and primary_brand in (host or ""))
        )
        entry["is_aggregator"] = is_aggregator_or_google_host(host)
        out.append(entry)
    return out


def _is_specific_geography(geo_text: str | None) -> bool:
    """True when a geography string names something tighter than a city.

    Surfaces the geography FLAG in the candidate table for geo-bound verticals
    (real-estate, restaurants, local services) - the agent applies the
    same-micro-market rule; code no longer drops anyone on it. False for
    global/regional/city-level profiles - keeps SaaS & D2C flows untouched.

    Heuristic (any of):
      - A whole-word marker token is present (road / street / nagar etc.)
      - A token ends with a compound locality suffix (-nagar, -halli, -pura)
    """
    if not geo_text:
        return False
    import re as _re
    tokens = set(_re.findall(r"[a-z0-9]+", geo_text.lower()))
    if tokens & _SPECIFIC_GEO_MARKER_WORDS:
        return True
    if any(t.endswith(sfx) for t in tokens for sfx in _SPECIFIC_GEO_COMPOUND_SUFFIXES):
        return True
    return False


# ─── URL resolution + fetch-verify (stage-2 helpers) ────────────────────────

async def _resolve_urls(candidates: list[dict[str, Any]], session_ctx: dict) -> None:
    """Candidate-stage URL fill (D-5, scoped back by CP-4): only candidates
    with a MISSING or aggregator URL get the locality-biased Google Business
    Profile lookup - candidate names here are often junk SEO page titles that
    can't pass the name guard, so a good search URL is left alone; the
    final-entry ladder (competitor_urls.resolve_project_url) revisits it with
    the clean analyst name.

    A displaced search URL survives as ``search_url`` - the fetch stage retries
    with it when the GBP site turns out dead - and a guard miss keeps it as
    ``url`` exactly as before, so the floor is the pre-Places behavior. A wrong
    URL is worse than none (it would poison the shared creative-library key);
    accepted URLs still pass fetch-verify."""
    for cand in candidates:
        url_host = host_of(cand.get("url"))
        if url_host and not is_aggregator_or_google_host(url_host):
            continue
        listing = await cached_business_listing(cand["name"], session_ctx)
        if not listing:
            continue
        if not listing_name_matches(cand["name"], listing["name"]):
            logger.info("places_url_rejected: name mismatch %r vs listing %r",
                        cand["name"], listing["name"])
            continue
        host = host_of(listing["website"])
        if not host or is_aggregator_or_google_host(host):
            logger.info("places_url_rejected: shared/aggregator host %s for %r",
                        host, cand["name"])
            continue
        logger.info("places_url_resolved: %r -> %s (search had %s)",
                    cand["name"], host, host_of(cand.get("url")) or "nothing")
        if cand.get("url"):
            cand["search_url"] = cand["url"]
        cand["url"] = listing["website"]
        cand["host"] = host


def _dedupe_resolved_hosts(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Post-resolution host dedup: two candidates can resolve to the same GBP
    website (extraction dedup ran on the pre-Places URLs). Keeps the first -
    the list arrives in the agent's pick order."""
    seen_hosts: set[str] = set()
    kept: list[dict[str, Any]] = []
    for cand in candidates:
        host = host_of(cand.get("url"))
        if host and host in seen_hosts:
            cand["dropped_dup_host"] = True  # shadow-table outcome marker
            logger.info("candidate_dedup_resolved: dropped %r (same host %s)",
                        cand["name"], host)
            continue
        if host:
            seen_hosts.add(host)
        kept.append(cand)
    return kept


async def _fetch_one_candidate(
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Run a web_fetch for one picked candidate.

    Two-step aggregator handling: if the landing page turns out to be an
    aggregator (directory, map search, marketplace), try to extract the brand's
    real URL from the page content and re-fetch it. Only drop the candidate
    when no usable brand URL can be recovered.

    Returns the candidate augmented with ``fetch_status`` (ok|failed|aggregator),
    ``fetch_answer``, and ``fetch_error``. Does NOT raise.
    """
    from app.agents.adzump.agents.product.adapters.web_fetch_adapter import (
        fetch_and_answer,
    )

    async def _one(url: str) -> dict[str, Any] | None:
        """Do one fetch, return None on any failure."""
        try:
            result = await asyncio.wait_for(
                fetch_and_answer(url, _FETCH_QUESTION_TEMPLATE),
                timeout=_FETCH_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            return {"_err": f"timeout after {int(_FETCH_TIMEOUT_SEC)}s"}
        except Exception as e:
            return {"_err": f"{type(e).__name__}: {str(e)[:160]}"}
        if not isinstance(result, dict) or result.get("status") != "ok":
            return {"_err": str(result.get("error", "unknown"))[:160]}
        return result

    url = candidate.get("url") or ""
    result = await _one(url)
    if result is None or result.get("_err"):
        # Places-first fallback (D-5): the GBP website was dead/blocked - retry
        # once with the search-derived URL it displaced, rather than dropping a
        # competitor we'd have kept before the inversion.
        search_url = candidate.get("search_url") or ""
        if search_url and host_of(search_url) != host_of(url):
            logger.info("candidate_fetch_fallback: %s dead, retrying %s",
                        host_of(url), host_of(search_url))
            url = search_url
            candidate = {**candidate, "url": search_url}
            result = await _one(url)
        if result is None or result.get("_err"):
            return {**candidate, "fetch_status": "failed",
                    "fetch_error": (result or {}).get("_err", "unknown")}

    answer = result.get("answer") or ""
    is_aggregator = answer.strip().upper().startswith("TYPE: AGGREGATOR")

    # If the first URL turned out to be an aggregator, try once to follow the
    # brand's official URL extracted from the aggregator page content.
    if is_aggregator:
        official_url = parse_official_url(answer)
        # Guard against redirect loops: the extracted URL must differ from the
        # one we just fetched, AND not itself be an aggregator.
        if official_url and host_of(official_url) and host_of(official_url) != host_of(url) \
                and not is_aggregator_or_google_host(host_of(official_url)):
            logger.info("candidate_fetch_redirect: %s -> %s", host_of(url), host_of(official_url))
            followup = await _one(official_url)
            if followup is not None and not followup.get("_err"):
                fa = followup.get("answer") or ""
                if not fa.strip().upper().startswith("TYPE: AGGREGATOR"):
                    return {
                        **candidate,
                        "fetch_status": "ok",
                        "fetch_answer": fa,
                        "fetch_url": followup.get("url"),
                        "fetch_title": followup.get("title"),
                        "resolved_from": url,
                    }
        # First URL was aggregator and we couldn't resolve a better one → drop.
        return {**candidate, "fetch_status": "aggregator", "fetch_answer": answer}

    return {
        **candidate,
        "fetch_status": "ok",
        "fetch_answer": answer,
        "fetch_url": result.get("url"),
        "fetch_title": result.get("title"),
    }


# ─── Search-result harvest ──────────────────────────────────────────────────

def _parse_web_search_result_block(
    block: dict[str, Any],
) -> list[dict[str, Any]]:
    """Extract candidates from a single Anthropic ``web_search_tool_result``.

    Anthropic's ``content`` is a union: ``list[web_search_result]`` on success,
    ``{type: web_search_tool_result_error, error_code: ...}`` on failure.
    Errors (e.g. ``max_uses_exceeded``) are logged and yield no candidates.
    """
    raw = block.get("content")
    tid = block.get("tool_use_id")

    if isinstance(raw, dict) and raw.get("type") == "web_search_tool_result_error":
        logger.warning(
            "web_search error tool_use_id=%s code=%s",
            tid, raw.get("error_code"),
        )
        return []
    if not isinstance(raw, list):
        logger.warning(
            "web_search_tool_result has unexpected content shape: type=%s",
            type(raw).__name__,
        )
        return []

    candidates: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("type") != "web_search_result":
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        candidates.append({
            "name": title,
            "url": item.get("url"),
            "summary": "",
            "relevance_note": "",
        })
    return candidates


def _extract_search_results_from_history(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Harvest Anthropic web_search results out of assistant message blocks.

    Each assistant turn can contain ``server_tool_use`` blocks (carrying the
    query) paired with ``web_search_tool_result`` blocks (carrying the hits,
    keyed by ``tool_use_id``). Convert them to the ``{query, candidates}``
    shape ``_merge_candidate_facts`` expects.
    """
    queries_by_id: dict[str, str] = {}
    hits_by_id: dict[str, list[dict[str, Any]]] = {}
    n_server_calls = n_result_blocks = 0

    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "server_tool_use" and block.get("name") == "web_search":
                n_server_calls += 1
                tid = str(block.get("id") or "")
                if tid:
                    queries_by_id[tid] = str((block.get("input") or {}).get("query") or "").strip()
            elif btype == "web_search_tool_result":
                n_result_blocks += 1
                tid = str(block.get("tool_use_id") or "")
                candidates = _parse_web_search_result_block(block)
                if candidates and tid:
                    hits_by_id.setdefault(tid, []).extend(candidates)

    total_candidates = sum(len(v) for v in hits_by_id.values())
    logger.info(
        "extract_search_results: server_tool_use=%d result_blocks=%d candidates=%d queries=%d",
        n_server_calls, n_result_blocks, total_candidates, len(hits_by_id),
    )

    return [
        {"query": queries_by_id.get(tid, ""), "candidates": cands}
        for tid, cands in hits_by_id.items()
    ]


# ─── Shadow candidate table ─────────────────────────────────────────────────

def _log_candidate_table(candidates: list[dict[str, Any]], picked_ids: set[str],
                         fetched: list[dict[str, Any]],
                         already_verified: set[str]) -> None:
    """Shadow table (CP-6): one structured line per fetch_candidates call
    capturing every pool candidate's facts, whether the AGENT picked it, and
    its outcome - the divergence/eval feedstock for hand-labeling agent picks.
    ``host`` is the SEARCH-derived host (pre-GBP; a displaced one is read back
    from ``search_url``). Every drop keeps its distinct reason - a host-dedup
    duplicate must not be labeled like a not-picked skip."""
    status_by_cid = {c.get("cid"): c.get("fetch_status") for c in fetched}
    rows = []
    for c in candidates:
        cid = c["cid"]
        outcome = status_by_cid.get(cid)
        if outcome is None:
            # Marker checks are gated on THIS call's picks - pool dicts keep
            # markers from earlier calls, and a stale one must not relabel a
            # merely not-picked candidate.
            if cid in already_verified:
                outcome = "verified_earlier"
            elif cid in picked_ids and c.get("dropped_dup_host"):
                outcome = "dup_host"
            elif cid in picked_ids and c.get("no_url"):
                outcome = "no_url"
            else:
                outcome = "not_picked"
        rows.append({
            "cid": c["cid"], "name": c["name"],
            "host": host_of(c.get("search_url")) or c.get("host") or "",
            "seen_in": len(c.get("seen_in") or []),
            "agg": bool(c.get("is_aggregator")),
            "picked": c["cid"] in picked_ids,
            "outcome": outcome,
        })
    logger.info("shortlist_candidate_table: %s", json.dumps(rows, ensure_ascii=False))


# ─── Tool definitions ───────────────────────────────────────────────────────

extract_candidates = ToolDefinition(
    name="extract_candidates",
    description=(
        "After your web_search queries, call this ONCE. It pools every search "
        "hit, dedupes by site/name, filters out the client's own business, and "
        "returns a fact table: ID, name, host, cross-search frequency, "
        "aggregator flag. No judgment happens here - YOU judge each candidate "
        "against the search content you already read, then call "
        "fetch_candidates with the chosen IDs."
    ),
    display_name="Extract Candidates",
    parameters=[],
    execute=_extract_candidates,
)


fetch_candidates = ToolDefinition(
    name="fetch_candidates",
    description=(
        "Verify and fetch the candidates you picked (by ID from "
        "extract_candidates). Resolves official URLs via Google Business "
        "lookup, dedupes hosts, fetches each page in parallel (following "
        "aggregator pages to the underlying brand site), and returns ID-keyed "
        "verified evidence. Pick the 6-8 strongest IDs (max 12); candidates "
        "whose fetch fails or that turn out to be aggregators are reported "
        "dropped. Already-verified IDs are skipped, never re-fetched."
    ),
    display_name="Fetch Candidates",
    parameters=[
        ToolParameter(
            name="ids",
            type="array",
            description="Candidate IDs to verify, e.g. [\"C1\", \"C4\", \"C7\"].",
            required=True,
            items={"type": "string"},
        ),
    ],
    execute=_fetch_candidates,
)
