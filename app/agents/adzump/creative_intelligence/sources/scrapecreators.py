"""``AdIntelligenceSource`` backed by scrapecreators.com (Meta Ad Library scrape).

Contract (https://docs.scrapecreators.com/v1/facebook/adLibrary/search/ads):
  - GET {base}/v1/facebook/adLibrary/search/ads - x-api-key auth; cursor paging;
    per-call credit metering (credits_charged/credits_remaining in the response).
  - Keyword search over Meta's ad library. Unlike a company query it returns ads
    from MANY pages, so this adapter picks the advertiser: the page whose ads
    link to the competitor's domain, else the page whose name matches, else the
    fetch is honestly empty - never another advertiser's creatives.
  - ``is_active``/``start_date``/``end_date`` are Meta's real values, not a
    crawl-lag heuristic (the reason this source replaced adlibrary.com).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import settings
from app.agents.adzump._shared import host_of
from app.agents.adzump.creative_intelligence.models import (
    Creative,
    MAX_CREATIVES_PER_COMPETITOR,
)
from app.agents.adzump.creative_intelligence.sources.base import SourceFetch

logger = logging.getLogger(__name__)

SEARCH_PATH = "/v1/facebook/adLibrary/search/ads"
PAGE_LIMIT = 3  # cursor pages per search; each page is one metered credit
# display_format -> our media_type; anything unknown falls back by asset shape.
_DISPLAY_FORMAT_MEDIA = {
    "VIDEO": "video",
    "MULTI_IMAGES": "carousel",
    "CAROUSEL": "carousel",
    "DCO": "carousel",
    "IMAGE": "image",
}


class ScrapeCreatorsError(Exception):
    """Non-recoverable scrapecreators.com failure (auth, credits, rate limit)."""


def _unix_to_iso(ts: Any) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (ValueError, OSError, TypeError):
        return ""


def _compact(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


class ScrapeCreatorsSource:
    """``AdIntelligenceSource`` backed by scrapecreators.com."""

    async def fetch(self, *, domain: str, name: str, country: str = "") -> SourceFetch:
        if not name:
            raise ScrapeCreatorsError(
                "scrapecreators needs a brand name - the search is keyword-based."
            )
        ads = await self._search_all(name=name, country=country)
        page_ads = _ads_of_the_advertiser(ads, domain=domain, name=name)
        creatives = [_to_creative(a) for a in page_ads[:MAX_CREATIVES_PER_COMPETITOR]]
        first = page_ads[0] if page_ads else {}
        snapshot = first.get("snapshot") or {}
        return SourceFetch(
            creatives=creatives,
            resolved_name=first.get("page_name") or "",
            logo_url=snapshot.get("page_profile_picture_url") or "",
            platform_ids={"page_id": first.get("page_id")} if first.get("page_id") else {},
        )

    # -- HTTP -----------------------------------------------------------------

    async def _search_all(self, *, name: str, country: str) -> list[dict]:
        """Exact-phrase search first (multi-word project names match junk
        unordered); one unordered retry when it finds nothing."""
        ads = await self._search_paged(name=name, country=country,
                                       search_type="keyword_exact_phrase")
        if not ads:
            ads = await self._search_paged(name=name, country=country,
                                           search_type="keyword_unordered")
        return ads

    async def _search_paged(self, *, name: str, country: str,
                            search_type: str) -> list[dict]:
        ads: list[dict] = []
        cursor = ""
        for _ in range(PAGE_LIMIT):
            data = await self._search_page(name=name, country=country,
                                           search_type=search_type, cursor=cursor)
            ads.extend(data.get("searchResults") or [])
            cursor = data.get("cursor") or ""
            if not cursor or len(ads) >= MAX_CREATIVES_PER_COMPETITOR:
                break
        return ads

    async def _search_page(self, *, name: str, country: str,
                           search_type: str, cursor: str) -> dict:
        key = settings.SCRAPECREATORS_API_KEY
        if not key:
            raise ScrapeCreatorsError("SCRAPECREATORS_API_KEY is not configured")

        params: dict[str, Any] = {
            "query": name,
            "search_type": search_type,
            "status": "ALL",  # full creative history; is_active marks the live ones
            "trim": "true",
        }
        if country:
            params["country"] = country.upper()
        if cursor:
            params["cursor"] = cursor

        base = settings.SCRAPECREATORS_BASE_URL.rstrip("/")
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(
                f"{base}{SEARCH_PATH}",
                headers={"x-api-key": key, "accept": "application/json"},
                params=params,
            )

        if resp.status_code == 401:
            raise ScrapeCreatorsError(
                "scrapecreators.com auth failed (401) - check SCRAPECREATORS_API_KEY")
        if resp.status_code == 402:
            raise ScrapeCreatorsError("scrapecreators.com out of credits (402)")
        if resp.status_code == 429:
            raise ScrapeCreatorsError("scrapecreators.com rate limited (429)")
        if resp.status_code != 200:
            raise ScrapeCreatorsError(
                f"scrapecreators.com search failed: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        logger.info(
            "scrapecreators_search: query=%r type=%s got=%d credits_left=%s",
            name, search_type, len(data.get("searchResults") or []),
            data.get("credits_remaining"),
        )
        return data


# -- Advertiser selection -----------------------------------------------------

def _ads_of_the_advertiser(ads: list[dict], *, domain: str, name: str) -> list[dict]:
    """A keyword search mixes advertisers; keep exactly one page's ads.

    Selection order: the page whose ads link to the competitor's ``domain``
    (catches parent-brand pages advertising the project microsite), else the
    page whose name matches the competitor, else nothing - a wrong advertiser's
    creatives must never enter the shared library."""
    by_page: dict[str, list[dict]] = {}
    for ad in ads:
        page_id = str(ad.get("page_id") or "")
        if page_id:
            by_page.setdefault(page_id, []).append(ad)
    if not by_page:
        return []

    host = (domain or "").lower()

    def links_to_domain(page_ads: list[dict]) -> bool:
        if not host:
            return False
        for ad in page_ads:
            link_host = host_of((ad.get("snapshot") or {}).get("link_url") or "")
            if link_host and host in link_host:
                return True
        return False

    name_compact = _compact(name)

    def name_matches(page_ads: list[dict]) -> bool:
        page_compact = _compact(page_ads[0].get("page_name") or "")
        return bool(name_compact) and bool(page_compact) and (
            name_compact in page_compact or page_compact in name_compact)

    groups = sorted(by_page.values(), key=len, reverse=True)
    chosen = (next((g for g in groups if links_to_domain(g)), None)
              or next((g for g in groups if name_matches(g)), None))
    logger.info(
        "scrapecreators_fetch: name=%r domain=%s pages=%d matched=%d page=%r",
        name, domain, len(by_page), len(chosen or []),
        (chosen or [{}])[0].get("page_name"),
    )
    return chosen or []


# -- Mapping: raw scrapecreators ad -> Creative --------------------------------

def _image_url(item: dict) -> str:
    return item.get("original_image_url") or item.get("resized_image_url") or ""


def _video_url(item: dict) -> str:
    return item.get("video_hd_url") or item.get("video_sd_url") or ""


def _to_creative(raw: dict) -> Creative:
    snapshot = raw.get("snapshot") or {}
    # Carousel/multi-image ads carry their media per CARD, not in the top-level
    # images/videos arrays - scan both, cards last as the fallback.
    cards = [c for c in (snapshot.get("cards") or []) if isinstance(c, dict)]
    videos = [v for v in (snapshot.get("videos") or []) if isinstance(v, dict)]
    videos += [c for c in cards if _video_url(c)]
    images = [i for i in (snapshot.get("images") or []) if isinstance(i, dict)]
    images += [c for c in cards if _image_url(c)]

    media_type = _DISPLAY_FORMAT_MEDIA.get(str(snapshot.get("display_format") or ""))
    if media_type is None:
        media_type = "video" if videos else "image"

    if media_type == "video" and videos:
        source_asset = _video_url(videos[0])
        poster = videos[0].get("video_preview_image_url") or ""
    else:
        source_asset = _image_url(images[0]) if images else ""
        poster = ""
        if not source_asset and videos:
            # display_format lied (e.g. MULTI_IMAGES with video-only cards) -
            # a playable video beats an empty card the renderer would skip.
            media_type = "video"
            source_asset = _video_url(videos[0])
            poster = videos[0].get("video_preview_image_url") or ""

    first_card = cards[0] if cards else {}
    card_body = first_card.get("body")
    card_text = (card_body.get("text") if isinstance(card_body, dict)
                 else card_body) or ""

    start, end = raw.get("start_date"), raw.get("end_date")
    days_running = 0
    try:
        if start and end:
            days_running = max(0, int((int(end) - int(start)) / 86400))
    except (TypeError, ValueError):
        pass

    return Creative(
        creative_id=str(raw.get("ad_archive_id") or ""),
        media_type=media_type,
        source_asset_url=source_asset,
        poster_source_url=poster,
        headline=snapshot.get("title") or first_card.get("title") or "",
        primary_text=(snapshot.get("body") or {}).get("text") or card_text,
        cta=snapshot.get("cta_text") or first_card.get("cta_text") or "",
        landing_url=snapshot.get("link_url") or first_card.get("link_url") or "",
        platform="meta",
        publisher_platforms=raw.get("publisher_platform") or [],
        first_seen=_unix_to_iso(start),
        last_seen=_unix_to_iso(end),
        is_active=bool(raw.get("is_active")),
        days_running=days_running,
        metrics={"estSpend": raw.get("spend") or 0},
    )
