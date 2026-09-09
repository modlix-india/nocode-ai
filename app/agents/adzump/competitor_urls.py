"""Competitor URL evidence helpers.

The GBP lookup (session-memoized), the acceptance guards (name matching,
aggregator/broker-host vetting), liveness, and project-page extraction - the
mechanical facts behind competitor URLs. The JUDGMENT lives with the
researcher: comp_discovery gathers these into per-candidate Official-URL
options and the analyst cites one by id (or null) in its final JSON. Nothing
here picks a URL; a post-hoc model must never judge what the full-context
researcher already judged.
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx

from app.agents.adzump._shared import host_of, is_aggregator_host

logger = logging.getLogger(__name__)

# Extends the shared AGGREGATOR_HOSTS with google.com - covers Maps citation
# URLs that show up in search results (google.com/maps/search/<brand>).
_AGGREGATOR_EXTRA_HOSTS: frozenset[str] = frozenset({"google.com"})

_LIVENESS_TIMEOUT_SECONDS = 4.0
_EXTRACTION_TIMEOUT_SECONDS = 20.0

_PROJECT_PAGE_QUESTION = (
    "ONE line only: write 'OFFICIAL_URL: <full url>' with the URL of this "
    "site's own dedicated page for the project '{name}' if such a page is "
    "explicitly linked on this page, or 'OFFICIAL_URL: none'. Do NOT invent "
    "a URL - only use one that appears on the page."
)

_NAME_SUFFIX_STRIPS = (
    " pvt ltd", " pvt. ltd.", " private limited", " ltd", " ltd.",
    " inc", " inc.", " llc", " gmbh", " corporation", " corp.", " corp",
    " co.", " company", " group",
)


async def cached_business_listings(name: str, session_ctx: dict) -> list[dict]:
    """Locality-biased GBP lookup, memoized per session - the candidate URL
    fill and the per-candidate Official-URL options share one Places call per
    name, misses included. Returns the RAW top-3 listings
    (``[{name, website}]``, website may be ""); acceptance guards run with
    each caller. Concurrent first lookups for one name can both miss and both
    call Places (benign: last write wins, cost of one duplicate call)."""
    from app.agents.adzump.adapters.google.maps import GoogleMapsClient

    cache: dict = session_ctx.setdefault("_places_listings_cache", {})
    key = normalize_business_name(name)
    if key in cache:
        return cache[key]
    place = (session_ctx.get("product_data") or {}).get("place") or {}
    listings = await GoogleMapsClient().find_business_listings(
        name, lat=place.get("lat"), lng=place.get("lng"))
    cache[key] = listings
    return listings


async def cached_business_listing(name: str, session_ctx: dict) -> dict | None:
    """The guarded top-1 view: the TOP listing when it has a website, else
    None (a lower listing's website is weaker identity evidence than the
    ranking says - the analyst weighs those via the options list)."""
    listings = await cached_business_listings(name, session_ctx)
    if listings and listings[0].get("website"):
        return listings[0]
    return None


# ─── GBP acceptance guards (shared with the candidate stage) ───────────────

def normalize_business_name(name: str) -> str:
    """Canonicalise a business name for matching/dedup. Lowercase, strip
    parenthetical glosses, punctuation, and common business-type suffixes.
    Parentheticals go FIRST: analysts emit "Purva Sparkling Springs
    (Puravankara)" - the gloss is a developer credit, not identity, and its
    tokens pollute brand matching if they survive into the normalized name
    (live 2026-09-04: a duplicated brand token inside "(...)" matched a dead
    clone domain)."""
    s = (name or "").lower().strip()
    s = re.sub(r"\([^)]*\)", " ", s)
    for suffix in _NAME_SUFFIX_STRIPS:
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def listing_name_matches(candidate_name: str, listing_name: str) -> bool:
    """Fuzzy same-business check between a candidate and a Places listing:
    compact-normalized containment either way ("Lodha Azur" matches
    "Lodha Azur by Lodha Group", not "Lodha Bellezza")."""
    a = normalize_business_name(candidate_name).replace(" ", "")
    b = normalize_business_name(listing_name).replace(" ", "")
    return bool(a) and bool(b) and (a in b or b in a)


def is_aggregator_or_google_host(host: str) -> bool:
    return is_aggregator_host(host, _AGGREGATOR_EXTRA_HOSTS)


def is_broker_style_tld(host: str) -> bool:
    """Kailash's prior (2026-09-04): in Indian real estate ~90% of sites that
    aren't plain .com/.in are broker lead-gen clones (.co.in, .info, .live
    swarms around every launch: nambiarvillasbannerghatta.co.in,
    nambiarbannerghatta.info). Such a host never becomes a citable
    Official-URL option - creatives still flow (the ad search is name-driven)
    and a user pin (url_source=user) bypasses this entirely."""
    if not host:
        return False
    host = host.split(":", 1)[0]
    return not (host.endswith(".com")
                or (host.endswith(".in") and not host.endswith(".co.in")))


def parse_official_url(answer: str) -> str | None:
    """Pull 'OFFICIAL_URL: <url>' out of a fetch answer. Returns None if
    missing, 'none', or not a valid-looking http(s) URL."""
    m = re.search(r"OFFICIAL_URL:\s*(\S+)", answer or "", flags=re.IGNORECASE)
    if not m:
        return None
    url = m.group(1).strip().rstrip(".,;")
    if not url or url.lower() == "none":
        return None
    if not (url.startswith("http://") or url.startswith("https://")):
        return None
    return url


# ─── Liveness + extraction ──────────────────────────────────────────────────

async def is_alive(url: str) -> bool:
    """Liveness only - content is GBP-trusted. HEAD, with one GET retry for
    servers that reject HEAD."""
    try:
        async with httpx.AsyncClient(
            timeout=_LIVENESS_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            response = await client.head(url)
            if response.status_code in (403, 405, 501):
                response = await client.get(url)
            return 200 <= response.status_code < 300
    except Exception:
        return False


async def project_page_from_site(
    name: str, listing_url: str, session_ctx: dict | None = None,
) -> str | None:
    """Ask a site for its own {name} project page - feeds the researcher's
    Official-URL options. Same-host guarded (a cross-host answer is a
    hallucination or an outbound link, either way not this site's project
    page) and session-memoized by (host, name), misses included - the answer
    depends only on the fetched page."""
    from app.agents.adzump.agents.product.adapters.web_fetch_adapter import (
        fetch_and_answer,
    )

    cache: dict | None = None
    cache_key = ""
    if session_ctx is not None:
        cache = session_ctx.setdefault("_project_page_cache", {})
        cache_key = f"{host_of(listing_url)}|{normalize_business_name(name)}"
        if cache_key in cache:
            return cache[cache_key]

    project_page: str | None = None
    try:
        result = await asyncio.wait_for(
            fetch_and_answer(listing_url, _PROJECT_PAGE_QUESTION.format(name=name)),
            timeout=_EXTRACTION_TIMEOUT_SECONDS,
        )
    except Exception:
        result = None
    if isinstance(result, dict) and result.get("status") == "ok":
        extracted = parse_official_url(result.get("answer") or "")
        if extracted and host_of(extracted) == host_of(listing_url):
            project_page = extracted
    if cache is not None:
        cache[cache_key] = project_page
    return project_page
