"""Client for the adlibrary.com ad-intelligence API.

adlibrary.com aggregates ~1B ads across Meta/TikTok/YouTube/Google/etc. and
exposes a REST search. We use it to pull a competitor's live ad creatives, then
normalize them into ``creative_library`` records for the shared store.

Contract (https://www.adlibrary.com/posts/api-documentation-and-implementation-guide):
  · POST {base}/search    — 1 credit/call; Bearer auth; appType "3" = brands
  · POST {base}/ad-detail — free; enriches one ad with reach/spend/audience
  · Auth header: ``Authorization: Bearer adl_...``
  · Limits: 10 req/min, 10k req/day. We page conservatively and stop early.

There's no "search by advertiser" endpoint, and the docs' ``independentWebsite``
(advertiser-domain) filter returns HTTP 500 on the live API — so we query by the
brand **keyword** (the competitor's name) and, when a domain is known, narrow the
results to ads whose ``independent_website`` matches it. The domain stays our
library dedup key.

Media URLs (``preview_img_url`` / ``video_url``) have no documented retention,
so callers should download the bytes into our own Files store rather than
persist these URLs (see the binaries pipeline / task #3).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.agents.adzump.services import creative_library as lib

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://adlibrary.com/api"
APP_TYPE_BRANDS = "3"  # Gaming="1", Apps="2", E-commerce/Brands="3"
DEFAULT_PAGE_SIZE = 60
# Bound how many ads we ingest per competitor: one prolific advertiser shouldn't
# burn the daily credit budget or overflow a library record. Aligns with the
# library's per-competitor creative cap.
DEFAULT_MAX_RESULTS = lib.MAX_CREATIVES_PER_COMPETITOR

# adlibrary.com ads_type → our media_type
_MEDIA_TYPE = {1: "image", 2: "video", 3: "carousel", 4: "collection"}
# An ad whose last_seen is within this many days is treated as still running.
# The API has no explicit active flag, so this is a heuristic.
_ACTIVE_WINDOW_DAYS = 7


class AdLibraryError(Exception):
    """Raised on a non-recoverable adlibrary.com API failure (auth, credits)."""


def _api_key() -> str | None:
    return getattr(settings, "ADLIBRARY_API_KEY", None)


def _base_url() -> str:
    return getattr(settings, "ADLIBRARY_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _unix_to_iso(ts: Any) -> str:
    """adlibrary timestamps are unix seconds; store ISO-8601 like everything else."""
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (ValueError, OSError, TypeError):
        return ""


# ── HTTP ────────────────────────────────────────────────────────────────────


async def search_ads(
    *,
    keyword: str = "",
    platforms: list[str] | None = None,
    geo: list[str] | None = None,
    days_back: int = 90,
    sort_field: str = "-impression",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict:
    """One page of the /search endpoint. Returns the raw decoded JSON
    (``{results, total, page, pageSize, _credits}``). Raises ``AdLibraryError``
    on auth/credit failures so callers fail loud rather than silently empty.

    ``keyword`` is a free-text match across ad content — pass the brand name.
    (The docs' ``independentWebsite`` domain filter 500s on the live API, so we
    don't send it; callers narrow by domain after the fact.)"""
    key = _api_key()
    if not key:
        raise AdLibraryError("ADLIBRARY_API_KEY is not configured")

    body: dict[str, Any] = {
        "appType": APP_TYPE_BRANDS,
        "sortField": sort_field,
        "daysBack": days_back,
        "page": page,
        "pageSize": page_size,
    }
    if keyword:
        body["keyword"] = keyword
    if platforms:
        body["platform"] = platforms
    if geo:
        body["geo"] = geo

    import httpx

    headers = {
        "Authorization": key if key.lower().startswith("bearer ") else f"Bearer {key}",
        "Content-Type": "application/json",
        "accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{_base_url()}/search", headers=headers, json=body)

    if resp.status_code == 401:
        raise AdLibraryError("adlibrary.com auth failed (401) — check ADLIBRARY_API_KEY")
    if resp.status_code == 402:
        raise AdLibraryError("adlibrary.com out of credits (402)")
    if resp.status_code == 429:
        retry = resp.headers.get("Retry-After", "?")
        raise AdLibraryError(f"adlibrary.com rate limited (429), retry after {retry}s")
    if resp.status_code != 200:
        raise AdLibraryError(f"adlibrary.com search failed: {resp.status_code} {resp.text[:200]}")
    return resp.json()


async def fetch_competitor_ads(
    *,
    domain: str = "",
    name: str = "",
    platforms: list[str] | None = None,
    geo: list[str] | None = None,
    days_back: int = 90,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> list[dict]:
    """Page through /search for one competitor (by brand ``name``) and return
    the raw ad objects (up to ``max_results``). Stops at the result cap, the
    last page, or the first empty page.

    Queries by ``name`` (the only working query mode). When ``domain`` is given,
    results are narrowed to ads whose ``independent_website`` matches it — but
    only if at least one matches, so we don't drop everything when the API omits
    that field."""
    if not name:
        raise AdLibraryError(
            "fetch_competitor_ads needs a brand name — the API has no by-domain "
            "query (independentWebsite 500s)."
        )

    ads: list[dict] = []
    page = 1
    page_size = min(DEFAULT_PAGE_SIZE, max_results)
    while len(ads) < max_results:
        data = await search_ads(
            keyword=name, platforms=platforms, geo=geo,
            days_back=days_back, page=page, page_size=page_size,
        )
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
        # The advertiser domain comes back as ecom_advertiser_id (e.g.
        # "uk.gymshark.com"), so a substring match on the bare host catches
        # country subdomains too.
        narrowed = [a for a in ads if host in str(a.get("ecom_advertiser_id") or "").lower()]
        if narrowed:  # only narrow when we actually matched the advertiser
            matched = narrowed
    logger.info(
        "adlibrary_fetch: name=%s domain=%s fetched=%d matched=%d",
        name, domain, len(ads), len(matched),
    )
    return matched[:max_results]


# ── Normalization: adlibrary ad → creative_library records ───────────────────


def _is_active(last_seen_unix: Any) -> bool:
    """Heuristic 'still running': last_seen within the active window."""
    iso = _unix_to_iso(last_seen_unix)
    if not iso:
        return False
    seen = datetime.fromisoformat(iso)
    return (datetime.now(timezone.utc) - seen).days <= _ACTIVE_WINDOW_DAYS


def normalize_ad(raw: dict) -> dict:
    """Map one raw adlibrary ad object to a ``creative_library`` creative record.

    ``insights`` is left empty here — the derived analysis (angle/hook/tone/OCR)
    is computed downstream from the stored binary, not from the API payload."""
    # Field names below are the LIVE API's, which differ from the published docs
    # (e.g. call_to_action not button_text, body not message, resource_urls not
    # video_url, ecom_advertiser_id not independent_website).
    ads_type = raw.get("ads_type")
    media_type = _MEDIA_TYPE.get(ads_type, "image")
    preview_img = raw.get("preview_img_url") or ""
    resources = [u for u in (raw.get("resource_urls") or []) if isinstance(u, str)]

    if media_type == "video":
        # Video file lives in resource_urls when present; otherwise only the
        # still is available (video2pic ads). preview_img is always the poster.
        source_asset = (resources[0] if resources else "") or preview_img
        poster = preview_img
    else:
        # Image / carousel: prefer a full-res resource, else the preview image.
        source_asset = (resources[0] if resources else "") or preview_img
        poster = ""

    return lib.build_creative_record(
        creative_id=str(raw.get("ad_key") or ""),
        source_asset_url=source_asset,
        poster_source_url=poster,
        media_type=media_type,
        ad_copy={
            "headline": raw.get("title") or "",
            "primary_text": raw.get("body") or raw.get("message") or "",
            "cta": raw.get("call_to_action") or "",
            # landing URL isn't in the search payload (only has_store_url flag);
            # would require a /ad-detail enrichment call.
            "landing_url": "",
        },
        placement={
            "platform": raw.get("platform") or "",
            "format": "",  # not in search payload
            "publisher_platforms": raw.get("fb_merge_channel") or [],
        },
        lifecycle={
            "first_seen": _unix_to_iso(raw.get("first_seen")),
            "last_seen": _unix_to_iso(raw.get("last_seen")),
            "is_active": _is_active(raw.get("last_seen")),
            "days_running": raw.get("days_count") or 0,
            "variants": 0,  # related-ads count not in search payload
        },
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


def normalize_identity(domain: str, name: str, raw_ads: list[dict]) -> dict:
    """Derive the competitor identity dict from the queried key + the ad batch.
    Falls back across the advertiser/page name fields the API may populate."""
    first = raw_ads[0] if raw_ads else {}
    return {
        # Keep the queried (bare) domain as the key; ecom_advertiser_id carries
        # country subdomains (uk./de.) we don't want in the dedup key.
        "domain": domain or first.get("ecom_advertiser_id") or "",
        # page_name ("Gymshark Ltd") is cleaner than advertiser_name (the long
        # store title), so prefer it when we weren't given a name.
        "name": name or first.get("page_name") or first.get("advertiser_name") or "",
        "logo_url": first.get("logo_url") or "",
        # page_id isn't in the search payload (only has_page_id) — would need
        # a /ad-detail enrichment call.
        "platform_ids": {},
    }


def build_library_record(domain: str, name: str, raw_ads: list[dict]) -> dict:
    """Full pipeline: raw adlibrary search results → a stored library record.
    Empty ad list yields a ``fetchStatus="empty"`` record so freshness still
    advances (we don't re-hammer the API for a competitor that genuinely has
    no ads)."""
    creatives = [normalize_ad(a) for a in raw_ads]
    identity = normalize_identity(domain, name, raw_ads)
    return lib.build_competitor_record(
        identity, creatives, fetch_status="ok" if creatives else "empty",
    )
