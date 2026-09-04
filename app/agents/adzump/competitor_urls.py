"""Project-level competitor URL resolution (CP-4).

The required identity for a competitor entry is the PROJECT page - a dedicated
microsite or the project's page on the developer domain - never a bare brand
root or a category page. This module owns that ladder plus the GBP lookup
guards shared with the candidate stage (comp_discovery imports them back):

1. GBP lookup (session-memoized), guarded by name match + non-aggregator host
2. project-specific listing website (D-6 token test) + alive (HEAD 2xx) -> accept
3. non-project-specific listing site -> ask it for the project page URL
   (same-host guarded)
4. nothing found -> keep the best we hold (D-7 precedence)
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlparse

import httpx

from app.agents.adzump._shared import host_of, is_aggregator_host

logger = logging.getLogger(__name__)

# Extends the shared AGGREGATOR_HOSTS with google.com - covers Maps citation
# URLs that show up in search results (google.com/maps/search/<brand>).
_AGGREGATOR_EXTRA_HOSTS: frozenset[str] = frozenset({"google.com"})

# D-6: tokens too generic to prove a URL is about THIS project.
_GENERIC_URL_TOKENS: frozenset[str] = frozenset({
    "villa", "villas", "apartment", "apartments", "flat", "flats",
    "home", "homes", "luxury", "premium", "bhk", "road",
})
_MIN_DISTINCTIVE_TOKEN_LENGTH = 4

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


async def resolve_project_url(
    name: str, current_url: str | None, session_ctx: dict
) -> str | None:
    """The CP-4 ladder for one FINAL competitor entry (clean analyst name).

    Returns the entry's settled URL - may equal ``current_url``; None when the
    entry had none and nothing was found (the entry stays link-less, honestly).
    A ``current_url`` that already passes the D-6 token test short-circuits
    without spending a lookup.
    """
    current_url = (current_url or "").strip() or None
    if current_url and (is_aggregator_or_google_host(host_of(current_url))
                        or is_broker_style_tld(host_of(current_url))):
        # Aggregator pages and broker-style domains can never be the entry's
        # official URL - honestly link-less beats a clone.
        current_url = None
    if current_url and _is_project_specific(current_url, name, session_ctx):
        logger.info("project_url_kept: %r already project-specific (%s)",
                    name, current_url)
        return current_url

    listing = await cached_business_listing(name, session_ctx)
    website = ""
    if listing:
        if not listing_name_matches(name, listing["name"]):
            logger.info("project_url_rejected: name mismatch %r vs listing %r (%s)",
                        name, listing["name"], listing["website"])
        else:
            listing_host = host_of(listing["website"])
            if not listing_host or is_aggregator_or_google_host(listing_host):
                logger.info("project_url_rejected: shared/aggregator host %s for %r",
                            listing_host, name)
            elif is_broker_style_tld(listing_host):
                # Brokers claim GBP listings for projects; a broker-style
                # domain on the listing is a claimed profile, not identity.
                logger.info("project_url_rejected: broker-style domain %s for %r",
                            listing_host, name)
            else:
                website = listing["website"]
    if not website:
        logger.info("project_url_kept: %r no guard-passing GBP listing (%s)",
                    name, current_url or "no url")
        return current_url

    if _is_project_specific(website, name, session_ctx):
        if await _is_alive(website):
            logger.info("project_url_resolved: %r -> %s (rung 2: GBP project site)",
                        name, website)
            return website
        # A dead site answers nothing at rung 3 and loses at rung 4 - skip both.
        logger.info("project_url_kept: %r GBP site dead (%s)",
                    name, current_url or "no url")
        return current_url

    project_page = await _project_page_from_site(name, website)
    if project_page:
        logger.info("project_url_resolved: %r -> %s (rung 3: listing-site extraction)",
                    name, project_page)
        return project_page

    # Rung 4 (D-7 keep-best): live GBP site beats a category/aggregator page;
    # a dead one keeps whatever the search stage found.
    if await _is_alive(website):
        logger.info("project_url_resolved: %r -> %s (rung 4: live GBP site)",
                    name, website)
        return website
    logger.info("project_url_kept: %r extraction found nothing (%s)",
                name, current_url or "no url")
    return current_url


async def cached_business_listing(name: str, session_ctx: dict) -> dict | None:
    """Locality-biased GBP lookup, memoized per session - both the candidate
    stage (comp_discovery) and the final-entry ladder share the cache, misses
    included, so a repeat round costs zero. Returns the RAW listing
    (``{name, website}`` or None); acceptance guards run with each caller."""
    from app.agents.adzump.adapters.google.maps import GoogleMapsClient

    cache: dict = session_ctx.setdefault("_places_website_cache", {})
    key = normalize_business_name(name)
    if key in cache:
        return cache[key]
    place = (session_ctx.get("product_data") or {}).get("place") or {}
    listing = await GoogleMapsClient().find_business_website(
        name, lat=place.get("lat"), lng=place.get("lng"))
    cache[key] = listing
    return listing


# ─── GBP acceptance guards (shared with the candidate stage) ───────────────

def normalize_business_name(name: str) -> str:
    """Canonicalise a business name for matching/dedup. Lowercase, strip
    parenthetical glosses, punctuation, and common business-type suffixes.
    Parentheticals go FIRST (CP-5 v2 step 13): analysts emit "Purva Sparkling
    Springs (Puravankara)" - the gloss is a developer credit, not identity,
    and its tokens defeat the D-6 brand exclusion (live 2026-09-04: a
    duplicated brand token inside "(...)" short-circuited the ladder onto a
    dead clone domain)."""
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
    nambiarbannerghatta.info). Such a host may not become an entry's OFFICIAL
    URL - creatives still flow (the ad search is name-driven) and a user pin
    (url_source=user) bypasses this entirely. A code-side prior the CP-6 judge
    can subsume later."""
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


# ─── D-6 project-specific test ──────────────────────────────────────────────

def _is_project_specific(url: str, name: str, session_ctx: dict) -> bool:
    """D-6: a distinctive project-name token appears in the HOST (a dedicated
    microsite), or in the PATH of a brand-owned host (the project's page on
    the developer domain). A project slug on a third-party host
    (propsoch.com/sobha-magnus) proves the page is ABOUT the project, not the
    project's own page. No distinctive tokens = unprovable = not specific."""
    tokens = _distinctive_tokens(name, session_ctx)
    if not tokens:
        return False
    parsed = urlparse(url)
    host_text = _compact(parsed.netloc)
    if any(_token_stem(token) in host_text for token in tokens):
        return True
    name_tokens = normalize_business_name(name).split()
    brand = name_tokens[0] if name_tokens else ""
    if not brand or _token_stem(brand) not in host_text:
        return False
    path_text = _compact(parsed.path)
    return any(_token_stem(token) in path_text for token in tokens)


def _distinctive_tokens(name: str, session_ctx: dict) -> list[str]:
    """Project-name tokens that can prove a URL is about THIS project: the
    leading brand token is dropped (it matches the developer's own root -
    'sobha' proves nothing about Sobha Magnus on sobha.com), as are generic
    real-estate words, campaign-address words, and short tokens."""
    place = (session_ctx.get("product_data") or {}).get("place") or {}
    address_tokens = set(re.findall(r"[a-z0-9]+", (place.get("address") or "").lower()))
    tokens = normalize_business_name(name).split()[1:]
    return [t for t in tokens
            if len(t) >= _MIN_DISTINCTIVE_TOKEN_LENGTH
            and t not in _GENERIC_URL_TOKENS
            and t not in address_tokens]


def _token_stem(token: str) -> str:
    """Singular/plural-tolerant prefix: 'springs' matches purvasparklingspring.com."""
    stem = token[:-1] if token.endswith("s") else token
    return stem if len(stem) >= _MIN_DISTINCTIVE_TOKEN_LENGTH else token


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


# ─── Rung helpers ───────────────────────────────────────────────────────────

async def _is_alive(url: str) -> bool:
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


async def _project_page_from_site(name: str, listing_url: str) -> str | None:
    """Rung 3: ask the GBP-listed site for its own {name} project page.
    Same-host guarded - a cross-host answer is a hallucination or an outbound
    link, either way not this site's project page. Not memoized (rare, and
    the answer depends only on the fetched page)."""
    from app.agents.adzump.agents.product.adapters.web_fetch_adapter import (
        fetch_and_answer,
    )

    try:
        result = await asyncio.wait_for(
            fetch_and_answer(listing_url, _PROJECT_PAGE_QUESTION.format(name=name)),
            timeout=_EXTRACTION_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    if not isinstance(result, dict) or result.get("status") != "ok":
        return None
    project_page = parse_official_url(result.get("answer") or "")
    if not project_page or host_of(project_page) != host_of(listing_url):
        return None
    return project_page
