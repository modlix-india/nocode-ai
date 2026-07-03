"""Search autosuggest adapter — real-query seed expansion from multiple engines.

Feeds the pipeline the actual phrasings people type, broadening the seed set before
the Keyword Planner scores volume and gates relevance. Google's suggestions are primary
for Search; other engines are coverage supplements selected by business context.

UNOFFICIAL endpoints — Google offers no public autosuggest API (verify periodically).
Each source is isolated and fail-soft: one that errors or times out is skipped, never
failing the pipeline. Any source can swap for a paid SERP API behind ``SuggestionSource``.

Endpoints (response shapes verified 2026-06-22):
  Google      https://suggestqueries.google.com/complete/search?client=firefox  -> JSON [query, [suggestions]]
  YouTube     same endpoint, ds=yt                                              -> JSON [query, [suggestions]]
  DuckDuckGo  https://ac.duckduckgo.com/ac/?type=list                           -> JSON [query, [suggestions]]
  Bing        https://www.bing.com/osjson.aspx                                  -> JSON [query, [suggestions]]
  Amazon      https://completion.amazon.com/search/complete                     -> JSON [_, [suggestions]]
  Brave       https://search.brave.com/api/suggest?rich=false                   -> JSON [query, [suggestions]]
All unofficial. Brave also has an *official* keyed + metered Suggest API
(api.search.brave.com) — not used here to keep every source keyless and free.
"""

from __future__ import annotations

import abc
import asyncio
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_AMAZON_MARKETPLACE_US = "1"   # Amazon ``mkt`` code (country→mkt mapping is a TODO)

# Autosuggest is a fast endpoint (~100-300ms); keep the per-call timeout tight so a
# slow source can't drag the pipeline. A timed-out call is treated as "no results".
_CALL_TIMEOUT_SECONDS = 3.0
_CONNECT_TIMEOUT_SECONDS = 2.0
_PER_CALL_TIMEOUT = httpx.Timeout(_CALL_TIMEOUT_SECONDS, connect=_CONNECT_TIMEOUT_SECONDS)
_DEFAULT_MAX_PER_SEED = 10            # suggestions kept per seed per source
_DEFAULT_CONCURRENCY = 10            # max in-flight autosuggest requests
_LOG_ERROR_MAXLEN = 120             # truncation for error messages in logs


def _parse_suggest_array(data: object) -> list[str]:
    """Parse the ``[query, [suggestions], ...]`` shape Google, YouTube, DuckDuckGo,
    and Amazon return."""
    if isinstance(data, list) and len(data) > 1 and isinstance(data[1], list):
        return [s for s in data[1] if isinstance(s, str) and s.strip()]
    return []


class SuggestionSource(abc.ABC):
    """One autosuggest engine. ``google_native`` flags Google's own surfaces, which
    are primary for a Google Search campaign (others are supplements). Adding a
    source is one subclass — no branching elsewhere."""

    name: str
    google_native: bool = False

    @abc.abstractmethod
    async def fetch(
        self, seed: str, *, hl: str, gl: str, client: httpx.AsyncClient
    ) -> list[str]:
        """Return raw suggestions for one seed. ``hl`` = language, ``gl`` = country."""
        raise NotImplementedError


class GoogleFamilySuggestSource(SuggestionSource):
    """Google's suggest endpoint (``client=firefox`` → flat JSON). The ``ds``
    (dataset) param: omitted defaults to **web** suggestions (equivalent to
    ``ds=web``, the proven form); ``ds="yt"`` is YouTube. Same ``[query,
    [suggestions]]`` shape, so one class serves both."""

    _URL = "https://suggestqueries.google.com/complete/search"

    def __init__(self, *, name: str, ds: str = "", google_native: bool = False):
        self.name = name
        self._ds = ds
        self.google_native = google_native

    async def fetch(self, seed, *, hl, gl, client):
        params = {"client": "firefox", "q": seed, "hl": hl, "gl": gl}
        if self._ds:
            params["ds"] = self._ds
        resp = await client.get(self._URL, params=params, timeout=_PER_CALL_TIMEOUT)
        resp.raise_for_status()
        return _parse_suggest_array(resp.json())


class DuckDuckGoSuggestSource(SuggestionSource):
    name = "duckduckgo"
    _URL = "https://ac.duckduckgo.com/ac/"

    async def fetch(self, seed, *, hl, gl, client):
        # ``type=list`` returns the same [query, [suggestions]] shape as Google.
        # Region (``kl``) uses a "{country}-{lang}" form; omitted here (returns
        # global suggestions) until that mapping is verified, to avoid sending a
        # malformed value.
        resp = await client.get(
            self._URL,
            params={"q": seed, "type": "list"},
            timeout=_PER_CALL_TIMEOUT,
        )
        resp.raise_for_status()
        return _parse_suggest_array(resp.json())


class BingSuggestSource(SuggestionSource):
    name = "bing"
    # qsml.aspx is dead (404); osjson returns the [query, [suggestions]] JSON shape.
    _URL = "https://www.bing.com/osjson.aspx"

    async def fetch(self, seed, *, hl, gl, client):
        resp = await client.get(
            self._URL,
            params={"query": seed, "mkt": f"{hl.lower()}-{gl.upper()}"},
            timeout=_PER_CALL_TIMEOUT,
        )
        resp.raise_for_status()
        return _parse_suggest_array(resp.json())


class BraveSuggestSource(SuggestionSource):
    """Brave Search's unofficial address-bar suggest endpoint. ``rich=false`` returns
    the flat ``[query, [suggestions]]`` shape (``rich=true`` returns objects we don't
    want). Independent index → web-search coverage like DuckDuckGo; geo is IP-based
    (no verified country param, so none sent). Brave's *official* keyed+metered Suggest
    API is the stable alternative if this one ever proves unreliable in production."""

    name = "brave"
    _URL = "https://search.brave.com/api/suggest"

    async def fetch(self, seed, *, hl, gl, client):
        resp = await client.get(
            self._URL,
            params={"q": seed, "rich": "false"},   # rich=true → objects, not strings
            timeout=_PER_CALL_TIMEOUT,
        )
        resp.raise_for_status()
        return _parse_suggest_array(resp.json())


class AmazonSuggestSource(SuggestionSource):
    """Commercial/product-intent suggestions — selected only for ecommerce/product
    businesses. ``mkt`` is the marketplace (US default; country→mkt mapping TODO)."""

    name = "amazon"
    _URL = "https://completion.amazon.com/search/complete"

    async def fetch(self, seed, *, hl, gl, client):
        resp = await client.get(
            self._URL,
            params={
                "client": "amazon-search-ui",
                "search-alias": "aps",
                "mkt": _AMAZON_MARKETPLACE_US,
                "q": seed,
            },
            timeout=_PER_CALL_TIMEOUT,
        )
        resp.raise_for_status()
        return _parse_suggest_array(resp.json())


# Source instances. ``google_native`` (Google web) is primary for a Search campaign;
# the rest are supplements the pipeline selects per BusinessProfile (Bing/DuckDuckGo/
# Brave = web-search coverage; Amazon = product intent; YouTube = informational).
GOOGLE = GoogleFamilySuggestSource(name="google", google_native=True)
YOUTUBE = GoogleFamilySuggestSource(name="youtube", ds="yt")
DUCKDUCKGO = DuckDuckGoSuggestSource()
BING = BingSuggestSource()
BRAVE = BraveSuggestSource()
AMAZON = AmazonSuggestSource()

# Name-keyed registry for the pipeline's data-driven, profile-based selection.
SOURCES: dict[str, SuggestionSource] = {
    s.name: s for s in (GOOGLE, YOUTUBE, DUCKDUCKGO, BING, BRAVE, AMAZON)
}
# Base web-search default. Brave is opt-in per product type (overlaps DuckDuckGo).
DEFAULT_SOURCES: list[SuggestionSource] = [GOOGLE, BING, DUCKDUCKGO]
DEFAULT_SOURCE_NAMES: tuple[str, ...] = tuple(s.name for s in DEFAULT_SOURCES)


async def fetch_suggestions(
    seeds: list[str],
    sources: Optional[list[SuggestionSource]] = None,
    *,
    hl: str = "en",
    gl: str = "US",
    max_per_seed: int = _DEFAULT_MAX_PER_SEED,
    concurrency: int = _DEFAULT_CONCURRENCY,
    client: Optional[httpx.AsyncClient] = None,
) -> list[str]:
    """Fan out ``seeds × sources`` concurrently and return de-duplicated suggestions
    with original order preserved.

    Never raises: a source/seed that fails is logged and skipped (fail-soft), so the
    pipeline degrades to whatever did return rather than erroring. ``concurrency``
    bounds in-flight requests; pass a shared ``client`` to reuse the connection pool.
    """
    sources = sources or DEFAULT_SOURCES
    if not seeds or not sources:
        return []

    owns_client = client is None
    client = client or httpx.AsyncClient()
    sem = asyncio.Semaphore(concurrency)

    async def _one(src: SuggestionSource, seed: str) -> tuple[str, list[str]]:
        async with sem:
            try:
                hits = (await src.fetch(seed, hl=hl, gl=gl, client=client))[:max_per_seed]
                return src.name, hits
            except Exception as e:  # fail-soft: one source/seed never breaks the run
                logger.info(
                    "autosuggest: source=%s seed=%r skipped: %s",
                    src.name, seed, str(e)[:_LOG_ERROR_MAXLEN],
                )
                return src.name, []

    try:
        results = await asyncio.gather(
            *(_one(src, seed) for src in sources for seed in seeds)
        )
    finally:
        if owns_client:
            await client.aclose()

    per_source: dict[str, int] = {}
    seen: set[str] = set()
    merged: list[str] = []
    for name, group in results:
        per_source[name] = per_source.get(name, 0) + len(group)
        for kw in group:
            norm = kw.strip().lower()
            if norm and norm not in seen:
                seen.add(norm)
                merged.append(kw.strip())
    logger.info(
        "autosuggest: %d seeds x %d sources -> per-source raw %s -> %d unique",
        len(seeds), len(sources),
        " ".join(f"{n}={c}" for n, c in per_source.items()) or "(none)", len(merged),
    )
    return merged
