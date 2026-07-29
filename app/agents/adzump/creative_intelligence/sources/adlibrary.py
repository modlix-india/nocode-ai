"""adlibrary.com adapter - one implementation of ``AdIntelligenceSource``.

adlibrary.com aggregates ~1B ads across Meta/TikTok/YouTube/Google/etc. and
exposes a REST search. This adapter queries it and maps its raw payload onto
``Creative`` objects; nothing outside this file knows the vendor's field names.

Contract (https://www.adlibrary.com/posts/api-documentation-and-implementation-guide):
  - POST {base}/search    - 1 credit/call; Bearer auth; appType "3" = brands
  - Limits: 10 req/min, 10k req/day. We page conservatively and stop early.

There's no "search by advertiser" endpoint, and the docs' ``independentWebsite``
(advertiser-domain) filter returns HTTP 500 on the live API - so we query by the
brand keyword (the competitor's name) and, when a domain is known, narrow results
to ads whose advertiser domain matches it. Media URLs have no documented
retention, so the library rehosts the bytes rather than persist these URLs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import settings
from app.agents.adzump.creative_intelligence.models import (
    Creative,
    MAX_CREATIVES_PER_COMPETITOR,
)
from app.agents.adzump.creative_intelligence.sources.base import SourceFetch

logger = logging.getLogger(__name__)

APP_TYPE_BRANDS = "3"  # Gaming="1", Apps="2", E-commerce/Brands="3"
DEFAULT_PAGE_SIZE = 60
# Lookback window for /search. The vendor's crawl of regional pages lags badly
# (measured 2026-07-27: "Purva Sparkling Springs" had 35 indexed ads, ALL seen
# 100+ days ago - daysBack=90 returned 0 of them while Meta showed the page
# live). A year-wide window surfaces what the vendor actually has; recency is
# judged downstream (is_active, winner_signal) from the per-ad timestamps.
DAYS_BACK = 365
# adlibrary.com ads_type -> our media_type
_MEDIA_TYPE = {1: "image", 2: "video", 3: "carousel", 4: "collection"}
# An ad whose last_seen is within this many days is treated as still running;
# the API has no explicit active flag, so this is a heuristic.
_ACTIVE_WINDOW_DAYS = 7


class AdLibraryError(Exception):
    """Non-recoverable adlibrary.com API failure (auth, credits, rate limit)."""


def _unix_to_iso(ts: Any) -> str:
    """adlibrary timestamps are unix seconds; store ISO-8601 like everything else."""
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (ValueError, OSError, TypeError):
        return ""


def _is_active(last_seen_unix: Any) -> bool:
    """Heuristic 'still running': last_seen within the active window."""
    iso = _unix_to_iso(last_seen_unix)
    if not iso:
        return False
    seen = datetime.fromisoformat(iso)
    return (datetime.now(timezone.utc) - seen).days <= _ACTIVE_WINDOW_DAYS


class AdLibrarySource:
    """``AdIntelligenceSource`` backed by adlibrary.com."""

    async def fetch(self, *, domain: str, name: str) -> SourceFetch:
        raw_ads = await self._fetch_ads(domain=domain, name=name)
        creatives = [self._to_creative(a) for a in raw_ads]
        first = raw_ads[0] if raw_ads else {}
        return SourceFetch(
            creatives=creatives,
            # page_name ("Gymshark Ltd") is cleaner than advertiser_name (the long
            # store title), so prefer it when we weren't given a name.
            resolved_name=name or first.get("page_name") or first.get("advertiser_name") or "",
            logo_url=first.get("logo_url") or "",
            platform_ids={},  # page_id isn't in the search payload (needs /ad-detail)
        )

    # -- HTTP -----------------------------------------------------------------

    async def _search(self, *, keyword: str, page: int, page_size: int) -> dict:
        """One page of /search. Raises ``AdLibraryError`` on auth/credit/rate
        failures so the caller fails loud rather than silently empty."""
        key = settings.ADLIBRARY_API_KEY
        if not key:
            raise AdLibraryError("ADLIBRARY_API_KEY is not configured")

        body: dict[str, Any] = {
            "appType": APP_TYPE_BRANDS,
            "sortField": "-impression",
            "daysBack": DAYS_BACK,
            "page": page,
            "pageSize": page_size,
        }
        if keyword:
            body["keyword"] = keyword

        headers = {
            "Authorization": key if key.lower().startswith("bearer ") else f"Bearer {key}",
            "Content-Type": "application/json",
            "accept": "application/json",
        }
        base = settings.ADLIBRARY_BASE_URL.rstrip("/")
        # Live /search latency is routinely ~35s (measured 2026-07-27), so 30s
        # timed out every real call; give it generous headroom.
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(f"{base}/search", headers=headers, json=body)

        if resp.status_code == 401:
            raise AdLibraryError("adlibrary.com auth failed (401) - check ADLIBRARY_API_KEY")
        if resp.status_code == 402:
            raise AdLibraryError("adlibrary.com out of credits (402)")
        if resp.status_code == 429:
            retry = resp.headers.get("Retry-After", "?")
            raise AdLibraryError(f"adlibrary.com rate limited (429), retry after {retry}s")
        if resp.status_code != 200:
            raise AdLibraryError(
                f"adlibrary.com search failed: {resp.status_code} {resp.text[:200]}"
            )
        return resp.json()

    async def _fetch_ads(self, *, domain: str, name: str) -> list[dict]:
        """Page /search for one competitor (by brand ``name``) up to the cap.
        When ``domain`` is given, narrow to ads whose advertiser domain matches -
        but only if at least one matches, so we don't drop everything when the API
        omits that field."""
        if not name:
            raise AdLibraryError(
                "adlibrary needs a brand name - the API has no by-domain query "
                "(independentWebsite 500s)."
            )
        max_results = MAX_CREATIVES_PER_COMPETITOR
        page_size = min(DEFAULT_PAGE_SIZE, max_results)
        ads: list[dict] = []
        page = 1
        while len(ads) < max_results:
            data = await self._search(keyword=name, page=page, page_size=page_size)
            batch = data.get("results") or []
            if not batch:
                break
            ads.extend(batch)
            total = int(data.get("total") or 0)
            if page * page_size >= total or len(batch) < page_size:
                break
            page += 1

        matched = ads
        if domain:
            host = domain.lower()
            # advertiser domain comes back as ecom_advertiser_id (e.g.
            # "uk.gymshark.com"); substring match catches country subdomains.
            narrowed = [a for a in ads if host in str(a.get("ecom_advertiser_id") or "").lower()]
            if narrowed:
                matched = narrowed
        logger.info(
            "adlibrary_fetch: name=%s domain=%s fetched=%d matched=%d",
            name, domain, len(ads), len(matched),
        )
        return matched[:max_results]

    # -- Mapping: raw adlibrary ad -> Creative --------------------------------

    def _to_creative(self, raw: dict) -> Creative:
        """Map one raw adlibrary ad onto a ``Creative``. Field names are the LIVE
        API's, which differ from the published docs (call_to_action not
        button_text, body not message, resource_urls not video_url,
        ecom_advertiser_id not independent_website)."""
        media_type = _MEDIA_TYPE.get(raw.get("ads_type"), "image")
        preview_img = raw.get("preview_img_url") or ""
        resources = [u for u in (raw.get("resource_urls") or []) if isinstance(u, str)]
        # Video: file lives in resource_urls when present, else only the still
        # (video2pic ads); preview_img is always the poster. Image/carousel:
        # prefer a full-res resource, else the preview.
        source_asset = (resources[0] if resources else "") or preview_img
        poster = preview_img if media_type == "video" else ""

        return Creative(
            creative_id=str(raw.get("ad_key") or ""),
            media_type=media_type,
            source_asset_url=source_asset,
            poster_source_url=poster,
            headline=raw.get("title") or "",
            primary_text=raw.get("body") or raw.get("message") or "",
            cta=raw.get("call_to_action") or "",
            # landing URL isn't in the search payload (only has_store_url flag).
            platform=raw.get("platform") or "",
            publisher_platforms=raw.get("fb_merge_channel") or [],
            first_seen=_unix_to_iso(raw.get("first_seen")),
            last_seen=_unix_to_iso(raw.get("last_seen")),
            is_active=_is_active(raw.get("last_seen")),
            days_running=int(raw.get("days_count") or 0),
            metrics={
                "impressions": raw.get("impression") or 0,
                "likes": raw.get("like_count") or 0,
                "comments": raw.get("comment_count") or 0,
                "shares": raw.get("share_count") or 0,
                "views": raw.get("view_count") or 0,
                "heat": raw.get("heat") or 0,
                "estSpend": raw.get("estimated_spend") or 0,
                "estSpendCurrency": raw.get("estimated_spend_currency") or "",
            },
        )
