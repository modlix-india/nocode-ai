"""Competitor-creative library — store of adlibrary.com ad creatives, keyed by competitor.

Scope is controlled by the ``CREATIVE_LIBRARY_SHARED`` setting:

* **Per-client (default, current):** the library lives under the logged-in
  client's own ``clientCode`` — like ``business_storage``, using the user's JWT
  directly. Simple, but each client builds its own library and may re-fetch a
  competitor another client already pulled.
* **Shared SYSTEM (future, flag on):** one ``CompetitorCreativeLibrary``
  collection owned by the ``SYSTEM`` client, read/written by every client via
  ``clientCode=SYSTEM`` — the gateway's ``AppDataService.clientCode`` resolver
  routes a subclient's call to the SYSTEM-owned DB, no token. A competitor
  researched once then serves all clients. Enabling it needs the SYSTEM-side
  storage created first; then flip the flag.

The only difference between the two is the clientCode — see
``_library_client_code``, the single seam. Binaries (ad images / video posters)
live in the Files service (handled by the binaries pipeline); each creative
record here just carries the resulting ``fileUrl``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from app.config import settings
from app.agents.adzump._shared import build_ds_headers
from app.agents.appbuilder.tools._shared import get_saas_client
# Shared gateway-envelope unwrap — the `result.result` / `content` quirk is
# gateway-wide, so we reuse the one implementation rather than drift a copy.
from app.agents.adzump.services.business_storage import _extract_records

logger = logging.getLogger(__name__)

STORAGE_NAME = "CompetitorCreativeLibrary"
APP_CODE = "marketingai"
# clientCode used only when CREATIVE_LIBRARY_SHARED is on — SYSTEM owns the
# shared collection (the universal hierarchy ancestor every client can reach).
LIBRARY_CLIENT_CODE = "SYSTEM"
SCHEMA_VERSION = 1
SOURCE = "adlibrary.com"

# How old a competitor's record may be before competitor analysis refreshes it.
DEFAULT_FRESHNESS_DAYS = 30
# Ceiling on creatives stored per competitor so one prolific advertiser can't
# blow the Mongo document size limit. High-volume competitors that hit this are
# a signal to split creatives into their own collection (see module TODO).
MAX_CREATIVES_PER_COMPETITOR = 60

READ_PAGE = "/api/core/function/execute/CoreServices.Storage/ReadPage"
CREATE = "/api/core/function/execute/CoreServices.Storage/Create"
UPDATE = "/api/core/function/execute/CoreServices.Storage/Update"


# ── Keys & time ─────────────────────────────────────────────────────────────


def competitor_key(domain_or_url: str) -> str:
    """Canonical dedup key for a competitor: the bare host, lowercased, no
    ``www.``. Two clients querying ``https://www.Nike.com/air`` and
    ``nike.com`` resolve to the same ``nike.com`` record. Matches how
    ``business_storage._normalize_url`` canonicalizes hosts."""
    if not domain_or_url:
        return ""
    raw = domain_or_url.strip()
    # urlparse needs a scheme to populate netloc; bare "nike.com" lands in path.
    parsed = urlparse(raw if "//" in raw else f"//{raw}")
    host = (parsed.netloc or parsed.path or "").lower().strip("/")
    return host.removeprefix("www.")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def freshness_days() -> int:
    """Staleness window, overridable via the ``CREATIVE_LIBRARY_FRESHNESS_DAYS``
    setting; falls back to the 30-day default."""
    return int(getattr(settings, "CREATIVE_LIBRARY_FRESHNESS_DAYS", DEFAULT_FRESHNESS_DAYS))


def is_stale(record: dict | None, max_age_days: int | None = None) -> bool:
    """True when ``record`` is missing or its ``lastFetchedAt`` is older than the
    freshness window — i.e. competitor analysis should refetch from adlibrary.com.
    Treats an unparseable/absent timestamp as stale (safer to refetch)."""
    if not record:
        return True
    fetched_at = (record.get("data") or record).get("lastFetchedAt") or record.get("lastFetchedAt")
    if not fetched_at:
        return True
    try:
        ts = datetime.fromisoformat(fetched_at)
    except (ValueError, TypeError):
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    window = timedelta(days=max_age_days if max_age_days is not None else freshness_days())
    return datetime.now(timezone.utc) - ts > window


# ── Headers ───────────────────────────────────────────────────────────────


def _library_client_code(ctx: dict) -> str:
    """The clientCode the library reads/writes under — the single scope seam.
    SYSTEM when ``CREATIVE_LIBRARY_SHARED`` is on (shared across all clients),
    otherwise the logged-in client's own code (per-client store)."""
    if getattr(settings, "CREATIVE_LIBRARY_SHARED", False):
        return LIBRARY_CLIENT_CODE
    return ctx.get("client_code", "")


def _library_headers(ctx: dict) -> dict[str, str]:
    """Headers for reading and writing the library. Uses the user's own JWT;
    sets ``clientCode`` to the library scope (per-client or SYSTEM)."""
    h = build_ds_headers(ctx)
    cc = _library_client_code(ctx)
    if cc:
        h["clientCode"] = cc
    h["AppCode"] = APP_CODE
    h["Content-Type"] = "application/json"
    return h


# ── Reads ───────────────────────────────────────────────────────────────


async def get_competitor(key: str, ctx: dict) -> dict | None:
    """Return the shared library record for a competitor key, or None on miss.
    ``key`` is a normalized host (see ``competitor_key``)."""
    key = competitor_key(key)
    if not key:
        return None
    payload = {
        "storageName": STORAGE_NAME,
        "appCode": APP_CODE,
        "clientCode": _library_client_code(ctx),
        "filter": {"field": "competitorKey", "value": key},
    }
    result = await get_saas_client().post(
        READ_PAGE, headers=_library_headers(ctx), json=payload,
    )
    if not result.success:
        logger.info("creative_library_read_miss: key=%s err=%s", key, result.error)
        return None
    records = _extract_records(result.data)
    return records[-1] if records else None


# ── Writes ────────────────────────────────────────────────────────────────


async def upsert_competitor(record: dict, ctx: dict) -> str | None:
    """Create or update a competitor's library record (keyed by
    ``competitorKey``). Latest fetch wins; returns the storage record id or None
    on failure. Mirrors ``business_storage.save_campaign``'s upsert shape."""
    key = record.get("competitorKey")
    if not key:
        logger.warning("creative_library_upsert_skipped: record has no competitorKey")
        return None

    existing = await get_competitor(key, ctx)
    if existing:
        existing_id = existing.get("_id") or existing.get("id")
        if existing_id:
            payload = {
                "storageName": STORAGE_NAME,
                "appCode": APP_CODE,
                "dataObjectId": existing_id,
                "dataObject": record,
                "isPartial": False,  # full refresh — replace stale creatives wholesale
            }
            result = await get_saas_client().post(
                UPDATE, headers=_library_headers(ctx), json=payload,
            )
            if not result.success:
                logger.warning("creative_library_update_failed: key=%s err=%s", key, result.error)
                return None
            logger.info("creative_library_ok: action=update key=%s id=%s", key, existing_id)
            return existing_id
        logger.warning("creative_library_upsert: existing record has no _id, creating fresh")

    payload = {"storageName": STORAGE_NAME, "appCode": APP_CODE, "dataObject": record}
    result = await get_saas_client().post(
        CREATE, headers=_library_headers(ctx), json=payload,
    )
    if not result.success:
        logger.warning("creative_library_create_failed: key=%s err=%s", key, result.error)
        return None
    new_records = _extract_records(result.data)
    new_id = (new_records[0].get("_id") or new_records[0].get("id") or "") if new_records else ""
    logger.info("creative_library_ok: action=create key=%s id=%s", key, new_id)
    return new_id or None


# ── Record construction (pure) ──────────────────────────────────────────────


def build_competitor_record(
    identity: dict, creatives: list[dict], *, fetch_status: str = "ok",
) -> dict[str, Any]:
    """Assemble the stored library record from a competitor identity dict and
    its creative records. Pure — no I/O. ``identity`` carries the human-facing
    + dedup fields; ``creatives`` are built by ``build_creative_record``.

    ``fetch_status`` ∈ {"ok", "empty", "error"} records why a record might have
    zero creatives (genuine empty vs. a failed fetch), so a later freshness
    pass can decide whether to retry."""
    key = competitor_key(identity.get("domain") or identity.get("key") or "")
    active = sum(1 for c in creatives if c.get("isActive"))
    return {
        # ── Identification ──
        "competitorKey": key,
        "name": identity.get("name", ""),
        "aliases": identity.get("aliases") or [],
        "domain": identity.get("domain", ""),
        # platform advertiser/page ids (e.g. {"metaPageId": "...", "fbPageId": "..."})
        "platformIds": identity.get("platform_ids") or {},
        "businessType": identity.get("business_type", ""),
        "location": identity.get("location", ""),
        "pricing": identity.get("pricing", ""),
        "logoUrl": identity.get("logo_url", ""),

        # ── Creatives + insights ──
        "creatives": creatives[:MAX_CREATIVES_PER_COMPETITOR],
        "totalCreatives": len(creatives),
        "activeCreatives": active,

        # ── Provenance / freshness ──
        "source": SOURCE,
        "lastFetchedAt": _now_iso(),
        "fetchStatus": fetch_status,
        "schemaVersion": SCHEMA_VERSION,
    }


def build_creative_record(
    *,
    creative_id: str,
    file_url: str = "",
    source_asset_url: str = "",
    poster_source_url: str = "",
    content_hash: str = "",
    media_type: str = "image",
    ad_copy: dict | None = None,
    placement: dict | None = None,
    lifecycle: dict | None = None,
    metrics: dict | None = None,
    insights: dict | None = None,
) -> dict[str, Any]:
    """One ad creative within a competitor record.

    For videos, ``source_asset_url`` is the video URL and ``poster_source_url``
    is the still/thumbnail (adlibrary's ``preview_img_url``); ``posterUrl`` holds
    the rehosted poster. For images, poster fields stay empty.

    Field groups (see design discussion):
      · link     — ids + the shared-Files ``file_url`` (binary) + dedup hash + poster
      · ad_copy  — headline / primaryText / description / cta / landingUrl
      · placement— platform / format / publisherPlatforms
      · lifecycle— firstSeen / lastSeen / isActive / daysRunning / variants
      · metrics  — impressions / spend / reach ranges (if the source exposes them)
      · insights — DERIVED by us (angle, offer, hook, tone, ocrText, colors), not
                   from the source; this is the analysis value-add.
    """
    ad_copy = ad_copy or {}
    placement = placement or {}
    lifecycle = lifecycle or {}
    return {
        "creativeId": creative_id,
        "fileUrl": file_url,
        "sourceAssetUrl": source_asset_url,
        # Video still/thumbnail: source + rehosted. Empty for images.
        "posterSourceUrl": poster_source_url,
        "posterUrl": "",
        "contentHash": content_hash,
        "mediaType": media_type,

        "headline": ad_copy.get("headline", ""),
        "primaryText": ad_copy.get("primary_text", ""),
        "description": ad_copy.get("description", ""),
        "cta": ad_copy.get("cta", ""),
        "landingUrl": ad_copy.get("landing_url", ""),

        "platform": placement.get("platform", ""),
        "format": placement.get("format", ""),
        "publisherPlatforms": placement.get("publisher_platforms") or [],

        "firstSeen": lifecycle.get("first_seen", ""),
        "lastSeen": lifecycle.get("last_seen", ""),
        "isActive": bool(lifecycle.get("is_active", False)),
        "daysRunning": int(lifecycle.get("days_running") or 0),
        "variants": int(lifecycle.get("variants") or 0),

        "metrics": metrics or {},
        "insights": insights or {},
    }
