"""Competitor discovery tool — shortlist competitors from web search results.

Scores candidates on code signals (frequency, domain) + semantic signals
(format/geo/price match via batched classifier), fetches top-K candidate
pages in parallel, drops aggregators and fetch failures, returns verified
evidence block for the ProductAgent's final JSON.
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
    is_aggregator_host,
)

logger = logging.getLogger(__name__)

# Extends the shared AGGREGATOR_HOSTS with google.com — covers Maps citation
# URLs that show up in search results (google.com/maps/search/<brand>).
_AGGREGATOR_EXTRA: frozenset[str] = frozenset({"google.com"})


# Composite tool: reads stashed web_search results → scores candidates (code
# signals + one batched classifier call) → fetches top-K in parallel → drops
# failures/aggregators → returns merged evidence for the final JSON turn.

# Classifier is swappable between anthropic (default) and openai. The env-based
# kill switch (SHORTLIST_CLASSIFIER_PROVIDER) lets us revert quickly if Haiku
# underperforms on the batched format/geo/price classification prompt.
_SHORTLIST_CLASSIFIER_OPENAI_MODEL = "gpt-4o-mini"
_SHORTLIST_DEFAULT_MAX_FETCHES = 8
_SHORTLIST_MIN_COMPOSITE_SCORE = 3  # raised from 2 after reweight (Phase A)
_SHORTLIST_FETCH_TIMEOUT_SEC = 20.0


_NAME_SUFFIX_STRIPS = (
    " pvt ltd", " pvt. ltd.", " private limited", " ltd", " ltd.",
    " inc", " inc.", " llc", " gmbh", " corporation", " corp.", " corp",
    " co.", " company", " group",
)


def _normalize_name(name: str) -> str:
    """Canonicalise a brand name for dedup. Lowercase, strip punctuation and
    common business-type suffixes."""
    import re as _re
    s = (name or "").lower().strip()
    # Drop suffixes
    for suf in _NAME_SUFFIX_STRIPS:
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
    # Strip non-alphanumeric (keep spaces)
    s = _re.sub(r"[^a-z0-9\s]", " ", s)
    s = _re.sub(r"\s+", " ", s).strip()
    return s


# Local aliases over the shared helpers. ``_is_aggregator_host`` extends the
# shared set with google.com (Maps citation URLs) — specific to this tool.
_host_of = host_of


# Whole-word markers — a token equal to one of these signals a sub-city
# anchor. E.g. "Bannerghatta Road" has the token "road"; "JP Nagar Phase 5"
# has "nagar" AND "phase".
_SPECIFIC_GEO_MARKER_WORDS = frozenset({
    "road", "rd", "street", "st", "avenue", "ave", "boulevard", "blvd",
    "lane", "highway", "hwy",
    "neighborhood", "neighbourhood", "locality", "district",
    "zone", "quadrant", "quarter",
    "nagar", "layout", "colony", "extension", "phase", "sector", "block",
})

# Compound-suffix markers — localities whose name BAKES the marker into the
# word itself (common in Indian cities: "Indiranagar", "Koramangala",
# "JP Nagar" collapsed into "jpnagar", "Malleshpalya"). We match if a token
# ENDS with one of these.
_SPECIFIC_GEO_COMPOUND_SUFFIXES = (
    "nagar", "halli", "pura", "palya", "gudi", "pet", "layout", "colony",
)


def _is_specific_geography(geo_text: str | None) -> bool:
    """True when a geography string names something tighter than a city.

    Triggers the geo hard-floor for geo-bound verticals (real-estate,
    restaurants, local services). False for global/regional/city-level
    profiles — keeps SaaS & D2C flows untouched.

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


def _is_aggregator_host(host: str) -> bool:
    return is_aggregator_host(host, _AGGREGATOR_EXTRA)


async def _resolve_brand_url(name: str) -> str | None:
    """Try to find a brand's official URL by guessing common domain patterns.

    Tries ``brandname.com``, ``brandname.in``, ``brandname.co.in`` with a fast
    HEAD request. Returns the first that responds with HTTP 2xx/3xx, or None.
    Costs 0 LLM calls, runs in ~1-2s.
    """
    import re as _re
    import httpx

    # Normalize: "SNN Raj Viviente" → "snnrajviviente", "Sobha Galera" → "sobhagalera"
    slug = _re.sub(r"[^a-z0-9]", "", name.lower())
    if not slug or len(slug) < 3:
        return None

    # Also try with hyphens: "snn-raj-viviente"
    slug_hyphen = _re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

    candidates = []
    for s in dict.fromkeys([slug, slug_hyphen]):  # dedup, preserve order
        for suffix in (".com", ".in", ".co.in"):
            candidates.append(f"https://{s}{suffix}")
            candidates.append(f"https://www.{s}{suffix}")

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(4.0, connect=2.0),
        follow_redirects=True,
        verify=False,
    ) as client:
        for url in candidates:
            try:
                resp = await client.head(url)
                if resp.status_code < 400:
                    final_host = _host_of(str(resp.url))
                    if final_host and not _is_aggregator_host(final_host):
                        return str(resp.url)
            except Exception:
                continue
    return None


def _score_code_signals(
    search_results: list[dict[str, Any]], primary_host: str,
    primary_name: str = "",
) -> list[dict[str, Any]]:
    """Dedupe and score candidates on pure-code signals.

    Signals:
      +1 per search beyond the first the candidate appears in (frequency)
      +1 if URL host is distinct (not aggregator, not primary business)

    Self-reference detection uses both host (cityville.in) AND name fuzzy match
    (``Valmark CityVille`` ≈ ``Valmark City Ville``).
    """
    primary_name_norm = _normalize_name(primary_name)
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
            host = _host_of(url)
            # Prefer URL host as dedup key (same site = same brand); fall back
            # to normalized name so we still merge when the URL is missing.
            # Use brand name for dedup when URL is an aggregator/citation
            # (e.g. google.com/maps) — all such URLs share the same host,
            # which would incorrectly merge unrelated brands into one entry.
            key = host if (host and not _is_aggregator_host(host)) else _normalize_name(name)
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

    # Compute code score.
    out: list[dict[str, Any]] = []
    for entry in merged.values():
        freq_bonus = max(0, len(entry["seen_in"]) - 1)
        host = entry["host"]
        is_aggregator = _is_aggregator_host(host)
        # Self-reference: match on host OR fuzzy name overlap.
        # Compare with spaces stripped too ("cityville" vs "city ville").
        name_norm = _normalize_name(entry["name"])
        name_compact = name_norm.replace(" ", "")
        primary_compact = primary_name_norm.replace(" ", "")
        is_primary = (
            (bool(primary_host) and host == primary_host)
            or (bool(primary_compact) and len(primary_compact) > 3
                and (primary_compact in name_compact or name_compact in primary_compact))
        )
        domain_bonus = 1 if (host and not is_aggregator and not is_primary) else 0
        entry["code_score"] = freq_bonus + domain_bonus
        entry["is_primary"] = is_primary
        entry["is_aggregator"] = is_aggregator
        out.append(entry)
    return out


_CLASSIFIER_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "format_match": {"type": "boolean"},
                    "buyer_profile_match": {"type": "boolean"},
                    "geo_match": {"type": "boolean"},
                    "price_match": {"type": "boolean"},
                },
                "required": [
                    "name",
                    "format_match",
                    "buyer_profile_match",
                    "geo_match",
                    "price_match",
                ],
            },
        },
    },
    "required": ["classifications"],
}


_CLASSIFIER_PROMPT = """You are a strict market-fit classifier. Given a business profile and a list of candidate competitors, decide for each candidate whether it matches on FOUR dimensions. Be CONSERVATIVE — when in doubt, mark FALSE.

- format_match: TRUE if the candidate serves the same buyer need at the granularity the BUYER cares about. Use the BUYER'S lens, not the seller's label.
  * Real-estate: a luxury apartment and a luxury villament at similar price on the same road both serve "affluent-residence buyers" — format_match=TRUE. A 2 BHK budget apartment vs a 4 BHK luxury villament — format_match=FALSE (different buyers).
  * SaaS: a mid-market CRM and a mid-market helpdesk both serve "SMB customer-ops teams" — format_match=TRUE. A scheduling tool vs a CRM — format_match=FALSE (different jobs).
  * Restaurants: a premium North Indian restaurant vs a premium Italian restaurant in the same neighborhood at the same price — format_match=TRUE (same date-night buyer). A premium restaurant vs a fast-food chain — format_match=FALSE.

- buyer_profile_match: TRUE if the target CUSTOMER overlaps significantly — same demographics, same budget tier, same purchase trigger — INDEPENDENT of format.
  * Real-estate: Prestige Southern Star apartments ₹2-4 Cr and Valmark CityVille villaments ₹3-5 Cr both target affluent families 35-55 in South Bangalore — buyer_profile_match=TRUE even though one is apartment and one is villament.
  * SaaS: a CRM for 10-person startups vs a CRM for 1000-person enterprises — buyer_profile_match=FALSE even though both are CRMs.
  * D2C: a ₹1500 face serum and a ₹1500 face cream targeting the same skincare-conscious urban women — buyer_profile_match=TRUE.
  * If the candidate's target customer is unknown, buyer_profile_match=FALSE (don't give benefit of the doubt).

- geo_match: Match at the SAME GEOGRAPHIC SPECIFICITY the profile uses.
  * If the profile names a specific road / neighborhood / micro-market (e.g. "Bannerghatta Road, South Bangalore", "Indiranagar", "SoMa, San Francisco"), geo_match is TRUE only if the candidate is in that SAME road / neighborhood / quadrant. A candidate in a different part of the same city (e.g. North Bangalore vs South Bangalore, Koramangala vs Whitefield, Brooklyn vs Manhattan) is geo_match=FALSE — they serve different buyer pools.
  * If the profile's geography is only city-level (e.g. "Mumbai"), city-level candidates match.
  * If the profile's geography is regional ("South India", "EMEA") or national, match at that level.
  * If the profile has NO geographic anchor (pure online/global SaaS, D2C shipping worldwide), geo_match=TRUE for everyone.
  * If the candidate's geography is unknown/not mentioned, geo_match=FALSE (don't give benefit of the doubt).

- price_match: TRUE only if the candidate's pricing is within roughly ~30% of the profile's price tier. "Luxury ₹4 Cr villaments" and "₹80 L apartments" are NOT price matches even in the same area. If the candidate's pricing is unknown, price_match=FALSE.

Return one classification entry per candidate. Booleans only."""


async def _classify_via_anthropic(payload_json: str) -> dict:
    """One batched Claude Haiku call with json_schema structured output.

    Uses Anthropic's ``output_config`` (GA on Haiku 4.5) so the model is
    constrained to the schema — no post-hoc regex parsing. Runs the sync
    SDK in a thread to stay async-compatible with the rest of the tool.
    """
    import anthropic
    from app.config import settings

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    resp = await asyncio.to_thread(
        client.messages.create,
        model=settings.CLAUDE_HAIKU,
        max_tokens=4096,
        system=_CLASSIFIER_PROMPT,
        messages=[{"role": "user", "content": payload_json}],
        output_config={
            "format": {"type": "json_schema", "schema": _CLASSIFIER_SCHEMA},
        },
    )
    # Structured output lands in the first text block's parsed_output (GA
    # format). Fall back to parsing the text directly if for any reason
    # the SDK didn't populate it.
    for block in resp.content:
        if getattr(block, "type", None) != "text":
            continue
        parsed = getattr(block, "parsed_output", None)
        if isinstance(parsed, dict):
            return parsed
        text = getattr(block, "text", "") or ""
        if text:
            return json.loads(text)
    return {}


async def _classify_via_openai(payload_json: str) -> dict:
    """Legacy classifier path — kept behind the env kill switch."""
    from openai import AsyncOpenAI
    from app.config import settings

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    resp = await client.chat.completions.create(
        model=_SHORTLIST_CLASSIFIER_OPENAI_MODEL,
        messages=[
            {"role": "system", "content": _CLASSIFIER_PROMPT},
            {"role": "user", "content": payload_json},
        ],
        temperature=0,
        max_tokens=4096,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "candidate_classifications",
                "schema": _CLASSIFIER_SCHEMA,
                "strict": True,
            },
        },
    )
    raw = (resp.choices[0].message.content or "").strip()
    return json.loads(raw) if raw else {}


async def _classify_candidates(
    candidates: list[dict[str, Any]], profile_summary: str,
) -> dict[str, dict[str, bool]]:
    """One batched call classifying every candidate on the three
    semantic signals. Returns ``{normalized_name: {format_match, geo_match, price_match}}``.

    Provider selection: ``SHORTLIST_CLASSIFIER_PROVIDER`` env — ``anthropic``
    (default) uses Claude Haiku, ``openai`` keeps the legacy gpt-4o-mini path.

    Fail-soft: if the call errors, returns an empty dict — code_score alone
    still drives ranking. The shortlist tool won't block on classifier failure.
    """
    if not candidates:
        return {}

    import os

    user_payload = {
        "profile": profile_summary[:1500],
        "candidates": [
            {"name": c["name"], "summary": c.get("summary") or ""}
            for c in candidates
        ],
    }
    payload_json = json.dumps(user_payload)
    provider = (os.getenv("SHORTLIST_CLASSIFIER_PROVIDER") or "anthropic").lower()

    try:
        data = await (
            _classify_via_openai(payload_json)
            if provider == "openai"
            else _classify_via_anthropic(payload_json)
        )
    except Exception as e:
        logger.warning(
            "shortlist_classifier_failed provider=%s: %s: %s",
            provider, type(e).__name__, str(e)[:200],
        )
        return {}

    out: dict[str, dict[str, bool]] = {}
    for item in data.get("classifications") or []:
        if not isinstance(item, dict):
            continue
        name_key = _normalize_name(str(item.get("name") or ""))
        if not name_key:
            continue
        out[name_key] = {
            "format_match": bool(item.get("format_match")),
            "buyer_profile_match": bool(item.get("buyer_profile_match")),
            "geo_match": bool(item.get("geo_match")),
            "price_match": bool(item.get("price_match")),
        }
    return out


_FETCH_QUESTION_TEMPLATE = (
    "FIRST line: write either 'TYPE: BRAND' (this is the brand's own official site "
    "with info about ONE company/product) or 'TYPE: AGGREGATOR' (directory listing "
    "many unrelated brands, comparison portal, marketplace, map search, news article, "
    "or review site).\n"
    "SECOND line (only if AGGREGATOR): write 'OFFICIAL_URL: <url>' with the brand's "
    "own website URL if it is explicitly linked on this page, OR 'OFFICIAL_URL: none' "
    "if no such link is present. Do NOT invent a URL — only use one that appears on the page.\n\n"
    "Then answer: What does this page describe? What does the brand offer, where do "
    "they operate, what's their pricing or business model, and what stands out "
    "(trust signals, differentiators, target customer)?"
)


def _parse_official_url(answer: str) -> str | None:
    """Pull 'OFFICIAL_URL: <url>' out of a fetch answer. Returns None if
    missing, 'none', or not a valid-looking http(s) URL."""
    import re as _re
    m = _re.search(r"OFFICIAL_URL:\s*(\S+)", answer or "", flags=_re.IGNORECASE)
    if not m:
        return None
    url = m.group(1).strip().rstrip(".,;")
    if not url or url.lower() == "none":
        return None
    if not (url.startswith("http://") or url.startswith("https://")):
        return None
    return url


async def _fetch_one_for_shortlist(
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Run a web_fetch for one shortlist candidate.

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
                timeout=_SHORTLIST_FETCH_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            return {"_err": f"timeout after {int(_SHORTLIST_FETCH_TIMEOUT_SEC)}s"}
        except Exception as e:
            return {"_err": f"{type(e).__name__}: {str(e)[:160]}"}
        if not isinstance(result, dict) or result.get("status") != "ok":
            return {"_err": str(result.get("error", "unknown"))[:160]}
        return result

    url = candidate.get("url") or ""
    result = await _one(url)
    if result is None or result.get("_err"):
        return {**candidate, "fetch_status": "failed", "fetch_error": (result or {}).get("_err", "unknown")}

    answer = result.get("answer") or ""
    is_aggregator = answer.strip().upper().startswith("TYPE: AGGREGATOR")

    # If the first URL turned out to be an aggregator, try once to follow the
    # brand's official URL extracted from the aggregator page content.
    if is_aggregator:
        official_url = _parse_official_url(answer)
        # Guard against redirect loops: the extracted URL must differ from the
        # one we just fetched, AND not itself be an aggregator.
        if official_url and _host_of(official_url) and _host_of(official_url) != _host_of(url) \
                and not _is_aggregator_host(_host_of(official_url)):
            logger.info("shortlist_fetch_redirect: %s → %s", _host_of(url), _host_of(official_url))
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
    shape ``_shortlist_competitors`` expects.
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


async def _shortlist_competitors(params: dict, context: dict) -> ToolResult:
    """Score stashed search candidates, filter, parallel-fetch top-K, drop failures."""
    max_fetches = int(params.get("max_fetches") or _SHORTLIST_DEFAULT_MAX_FETCHES)
    max_fetches = max(1, min(max_fetches, 12))

    session_ctx = context.get("session_context") or {}
    research_state = session_ctx.setdefault("_research_state", {})
    search_results: list[dict] = research_state.get("search_results") or []

    # Anthropic server-side web_search doesn't stash results in session_context
    # (it happens inside the API, not in our Python). Fall back to parsing
    # message history for web_search_tool_result blocks.
    if not search_results:
        messages_getter = context.get("session_messages")
        messages = messages_getter() if callable(messages_getter) else (messages_getter or [])
        search_results = _extract_search_results_from_history(messages)
        if search_results:
            research_state["search_results"] = search_results

    if not search_results:
        return ToolResult(
            success=False,
            error="No search results available — run web_search first, then call shortlist_competitors.",
        )

    # Profile + primary-host context for scoring.
    business_brief = session_ctx.get("product_profile") or {}
    profile_summary = business_brief.get("summary") or ""
    primary_url = business_brief.get("url") or ""
    primary_host = _host_of(primary_url)

    # Layer A: code signals.
    await emit_progress(context, "Scoring candidates…")
    product_name = (session_ctx.get("product_data") or {}).get("product_name", "")
    scored = _score_code_signals(search_results, primary_host, primary_name=product_name)
    # Filter only self-references. Aggregator-URL candidates are kept — they go
    # through URL resolution + aggregator-follow fetch to recover their real URLs.
    pre_filtered = [c for c in scored if not c["is_primary"]]

    # Layer B: one batched classifier call.
    await emit_progress(context,f"Classifying {len(pre_filtered)} candidates…")
    classifications = await _classify_candidates(pre_filtered, profile_summary)

    # Composite score — weights prioritize location + buyer-pool over
    # granular format. Format becomes a tie-breaker instead of a gate.
    # Rationale: a luxury apartment at the same price on the same road
    # IS a competitor for a villament; a villa in the wrong city is NOT.
    # See _CLASSIFIER_PROMPT for the vertical-agnostic definitions.
    specific_geo = _is_specific_geography(profile_summary)
    for cand in pre_filtered:
        key = _normalize_name(cand["name"])
        sig = classifications.get(key) or {}
        cand["format_match"] = bool(sig.get("format_match"))
        cand["buyer_profile_match"] = bool(sig.get("buyer_profile_match"))
        cand["geo_match"] = bool(sig.get("geo_match"))
        cand["price_match"] = bool(sig.get("price_match"))
        cand["composite_score"] = (
            cand["code_score"]
            + 3 * int(cand["geo_match"])              # location first
            + 2 * int(cand["buyer_profile_match"])    # then buyer pool
            + 1 * int(cand["price_match"])
            + 1 * int(cand["format_match"])           # format is tie-breaker
        )

    # Geo hard-floor: for geo-bound profiles (specific road/neighborhood),
    # exclude candidates that miss BOTH geo_match AND buyer_profile_match —
    # they aren't competing for the same buyer pool. They can still appear
    # via ALTERNATIVE (not a full drop) but won't dominate DIRECT.
    if specific_geo:
        geo_excluded = [
            c for c in pre_filtered
            if not c["geo_match"] and not c["buyer_profile_match"]
        ]
        pre_filtered = [
            c for c in pre_filtered
            if c["geo_match"] or c["buyer_profile_match"]
        ]
        if geo_excluded:
            logger.info(
                "shortlist_geo_floor: excluded %d wrong-geo+wrong-buyer candidates "
                "(specific geography detected in profile)",
                len(geo_excluded),
            )

    # Rank + filter for fetch.
    pre_filtered.sort(key=lambda c: c["composite_score"], reverse=True)
    above_threshold = [
        c for c in pre_filtered
        if c["composite_score"] >= _SHORTLIST_MIN_COMPOSITE_SCORE
    ][:max_fetches + 4]  # take a few extra in case URL resolution fails for some

    # Let ALL candidates with URLs through to the fetch stage — including
    # Maps/aggregator URLs. The aggregator-follow path in _fetch_one_for_shortlist
    # extracts the official URL from the page and re-fetches.
    fetch_candidates = [c for c in above_threshold if c.get("url")][:max_fetches]

    if not fetch_candidates:
        scored_count = len(pre_filtered)
        return ToolResult(
            success=False,
            error=(
                f"No candidates passed filter: {scored_count} scored, 0 above threshold "
                f"(min composite score {_SHORTLIST_MIN_COMPOSITE_SCORE}) with a valid URL."
            ),
        )

    # Parallel fetch.
    await emit_progress(context,f"Fetching {len(fetch_candidates)} competitor pages in parallel…")
    fetched = await asyncio.gather(
        *(_fetch_one_for_shortlist(c) for c in fetch_candidates),
        return_exceptions=False,
    )

    # Partition by status.
    verified = [c for c in fetched if c.get("fetch_status") == "ok"]
    aggregator_drops = [c for c in fetched if c.get("fetch_status") == "aggregator"]
    fetch_fails = [c for c in fetched if c.get("fetch_status") == "failed"]

    logger.info(
        "shortlist_competitors: scored=%d classified=%d fetched=%d dropped=%d "
        "(aggregator=%d fetch_fail=%d) specific_geo=%s "
        "signals=(fmt=%d buyer=%d geo=%d price=%d)",
        len(pre_filtered), len(classifications), len(fetched),
        len(aggregator_drops) + len(fetch_fails),
        len(aggregator_drops), len(fetch_fails),
        specific_geo,
        sum(1 for c in pre_filtered if c.get("format_match")),
        sum(1 for c in pre_filtered if c.get("buyer_profile_match")),
        sum(1 for c in pre_filtered if c.get("geo_match")),
        sum(1 for c in pre_filtered if c.get("price_match")),
    )

    # Stash verified list for downstream visibility.
    research_state["verified_competitors"] = verified

    # Build evidence block.
    if not verified:
        lines = [
            "## Shortlist Competitors — ALL FETCHES FAILED OR WERE AGGREGATORS",
            "",
            f"Scored {len(pre_filtered)} candidates, fetched top {len(fetched)}, "
            f"kept 0. ({len(aggregator_drops)} were aggregators, {len(fetch_fails)} failed).",
            "",
            "Write the final JSON with an empty competitors array and a note in `notes` "
            "explaining that no competitors could be verified.",
        ]
        return ToolResult(
            success=True,
            data={"verified": verified, "dropped": {"aggregator": aggregator_drops, "fetch_fail": fetch_fails}},
            summary="\n".join(lines),
        )

    lines: list[str] = [
        f"## Verified Competitors ({len(verified)})",
        "",
        f"Scored {len(pre_filtered)} candidates; fetched top {len(fetched)}; "
        f"kept {len(verified)} after dropping {len(aggregator_drops)} aggregators "
        f"and {len(fetch_fails)} fetch failures.",
        "",
    ]
    for c in verified:
        lines.append(f"### {c['name']}")
        if c.get("url"):
            lines.append(f"URL: {c['url']}")
        fmt = bool(c.get("format_match"))
        buyer = bool(c.get("buyer_profile_match"))
        geo = bool(c.get("geo_match"))
        price = bool(c.get("price_match"))
        # Segment hint. For geo-bound businesses (specific road/neighborhood),
        # location is the dominant signal — any verified competitor on the same
        # road is head-to-head, regardless of format or price-tier misses from
        # the classifier. For non-geo-bound businesses (SaaS, D2C), require
        # format or buyer match alongside geo for DIRECT.
        same_pool = fmt or buyer
        if specific_geo:
            if geo:
                segment_hint = "DIRECT"
            elif same_pool:
                segment_hint = "ADJACENT"
            else:
                segment_hint = "ALTERNATIVE"
        else:
            if geo and same_pool:
                segment_hint = "DIRECT"
            elif geo or same_pool:
                segment_hint = "ADJACENT"
            else:
                segment_hint = "ALTERNATIVE"
        lines.append(
            f"SEGMENT: {segment_hint}  "
            f"(format={'yes' if fmt else 'no'} "
            f"buyer={'yes' if buyer else 'no'} "
            f"geo={'yes' if geo else 'no'} "
            f"price={'yes' if price else 'no'})"
        )
        if c.get("summary"):
            lines.append(f"Snippet: {c['summary']}")
        ans = (c.get("fetch_answer") or "").strip()
        if ans.upper().startswith("TYPE: BRAND"):
            ans = ans[len("TYPE: BRAND"):].lstrip(":\n ").strip()
        if ans:
            lines.append("")
            lines.append(f"Answer: {ans}")
        lines.append("")

    if fetch_fails or aggregator_drops:
        lines.append("---")
        lines.append("")
        if fetch_fails:
            lines.append(f"Dropped due to fetch failure ({len(fetch_fails)}): " +
                         ", ".join(c["name"] for c in fetch_fails))
        if aggregator_drops:
            lines.append(f"Dropped as aggregator ({len(aggregator_drops)}): " +
                         ", ".join(c["name"] for c in aggregator_drops))

    return ToolResult(
        success=True,
        data={"verified": verified, "dropped": {"aggregator": aggregator_drops, "fetch_fail": fetch_fails}},
        summary="\n".join(lines),
    )


shortlist_competitors = ToolDefinition(
    name="shortlist_competitors",
    description=(
        "After at least 5 web_search calls, score all surfaced candidates "
        "deterministically (frequency + domain + format/geo/price match via a "
        "batched classifier), filter aggregators, and fetch the top 6-8 brand "
        "pages in parallel. Drops candidates whose fetch fails or returns an "
        "aggregator page. Returns a structured evidence block you can transcribe "
        "into the final JSON. Call this ONCE after your web_search queries."
    ),
    display_name="Shortlist Competitors",
    parameters=[
        ToolParameter(
            name="max_fetches",
            type="integer",
            description="Max number of candidate pages to fetch (default 8, max 12).",
            required=False,
        ),
    ],
    execute=_shortlist_competitors,
)
