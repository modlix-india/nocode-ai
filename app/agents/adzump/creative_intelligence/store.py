"""Persistence for the competitor-creative library, keyed by competitor.

Reads validate the stored record straight into a ``Competitor``; writes persist
``Competitor.model_dump(by_alias=True)``. One shape crosses the boundary in each
direction - callers never see the raw envelope or a half-nested dict.

Scope is the single ``CREATIVE_LIBRARY_SHARED`` seam (``_library_client_code``):
per-client today (the logged-in client's own code, like business_storage), or one
SYSTEM-owned collection every client reads once the shared storage is provisioned.
Binaries (images / video posters) live in the Files service; each ``Creative``
carries the resulting ``fileUrl`` / ``posterUrl``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from app.config import settings
from app.agents.adzump._shared import (
    STORAGE_CREATE,
    STORAGE_READ_PAGE,
    STORAGE_UPDATE,
    extract_storage_records,
    host_of,
    storage_headers,
)
from app.agents.appbuilder.tools._shared import get_saas_client
from app.agents.adzump.creative_intelligence.models import Competitor

logger = logging.getLogger(__name__)

STORAGE_NAME = "CompetitorCreativeLibrary"
APP_CODE = "marketingai"
# clientCode used only when CREATIVE_LIBRARY_SHARED is on - SYSTEM owns the shared
# collection (the universal hierarchy ancestor every client can reach).
LIBRARY_CLIENT_CODE = "SYSTEM"

DEFAULT_FRESHNESS_DAYS = 30
# An "empty" record is a retry-soon marker, not a month of "this brand runs no
# ads" - keyword search results are noisy, so refetch empties the next day.
EMPTY_RECORD_FRESHNESS_DAYS = 1


# ── Keys & freshness (pure) ──────────────────────────────────────────────────


def competitor_key(domain_or_url: str) -> str:
    """Canonical dedup key: the bare host, lowercased, no ``www.`` - the shared
    ``host_of`` normalization, so the dedup key can never diverge from how the
    rest of the competitor pipeline canonicalizes hosts. Two clients querying
    ``https://www.Nike.com/air`` and ``nike.com`` resolve to the same record."""
    raw = (domain_or_url or "").strip()
    if not raw:
        return ""
    # urlparse needs a scheme to populate netloc; bare "nike.com" lands in path.
    return host_of(raw if "//" in raw else f"//{raw}")


def freshness_days() -> int:
    return int(settings.CREATIVE_LIBRARY_FRESHNESS_DAYS or DEFAULT_FRESHNESS_DAYS)


def is_stale(competitor: Competitor | None, max_age_days: int | None = None) -> bool:
    """True when the record is missing or its ``last_fetched_at`` is older than the
    freshness window - i.e. we should refetch from the source. An unparseable or
    absent timestamp counts as stale (safer to refetch)."""
    if competitor is None or not competitor.last_fetched_at:
        return True
    try:
        ts = datetime.fromisoformat(competitor.last_fetched_at)
    except (ValueError, TypeError):
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    days = max_age_days if max_age_days is not None else freshness_days()
    if competitor.fetch_status == "empty":
        days = min(days, EMPTY_RECORD_FRESHNESS_DAYS)
    return datetime.now(timezone.utc) - ts > timedelta(days=days)


# ── Headers ──────────────────────────────────────────────────────────────────


def _library_client_code(ctx: dict) -> str:
    """The clientCode the library reads/writes under - the single scope seam."""
    if settings.CREATIVE_LIBRARY_SHARED:
        return LIBRARY_CLIENT_CODE
    return ctx.get("client_code", "")


def _library_headers(ctx: dict) -> dict[str, str]:
    return storage_headers(ctx, APP_CODE, client_code=_library_client_code(ctx))


# ── Reads ──────────────────────────────────────────────────────────────────


async def get_competitor(key: str, ctx: dict) -> Competitor | None:
    """Return the stored ``Competitor`` for a key, or None on miss."""
    competitor, _ = await _read(key, ctx)
    return competitor


async def _read(key: str, ctx: dict) -> tuple[Competitor | None, str | None]:
    """Read the record and its storage id (the id is needed only for upsert).
    Returns (Competitor, record_id) or (None, None) on miss."""
    key = competitor_key(key)
    if not key:
        return None, None
    payload = {
        "storageName": STORAGE_NAME,
        "appCode": APP_CODE,
        "clientCode": _library_client_code(ctx),
        "filter": {"field": "competitorKey", "value": key},
    }
    result = await get_saas_client().post(
        STORAGE_READ_PAGE, headers=_library_headers(ctx), json=payload)
    if not result.success:
        logger.info("creative_library_read_miss: key=%s err=%s", key, result.error)
        return None, None
    records = extract_storage_records(result.data)
    if not records:
        return None, None
    record = records[-1]
    record_id = record.get("_id") or record.get("id")
    try:
        return Competitor.model_validate(record), record_id
    except ValidationError as e:
        # A record another writer version corrupted must stay recoverable: treat
        # it as a miss (stale -> refetch) but keep the id so the upsert can
        # overwrite it instead of permanently poisoning the key.
        logger.warning("creative_library_invalid_record: key=%s err=%s",
                       key, str(e)[:200])
        return None, record_id


# ── Writes ─────────────────────────────────────────────────────────────────


async def upsert_competitor(competitor: Competitor, ctx: dict) -> str | None:
    """Create or update a competitor's record (keyed by ``competitorKey``). Latest
    fetch wins. Returns the storage record id, or None on failure."""
    key = competitor.competitor_key
    if not key:
        logger.warning("creative_library_upsert_skipped: record has no competitorKey")
        return None

    record = competitor.model_dump(by_alias=True)
    _, existing_id = await _read(key, ctx)
    if existing_id:
        payload = {
            "storageName": STORAGE_NAME,
            "appCode": APP_CODE,
            "dataObjectId": existing_id,
            "dataObject": record,
            "isPartial": False,  # full refresh - replace stale creatives wholesale
        }
        result = await get_saas_client().post(
            STORAGE_UPDATE, headers=_library_headers(ctx), json=payload)
        if not result.success:
            logger.warning("creative_library_update_failed: key=%s err=%s", key, result.error)
            return None
        logger.info("creative_library_ok: action=update key=%s id=%s", key, existing_id)
        return existing_id

    payload = {"storageName": STORAGE_NAME, "appCode": APP_CODE, "dataObject": record}
    result = await get_saas_client().post(
        STORAGE_CREATE, headers=_library_headers(ctx), json=payload)
    if not result.success:
        logger.warning("creative_library_create_failed: key=%s err=%s", key, result.error)
        return None
    new_records = extract_storage_records(result.data)
    new_id = (new_records[0].get("_id") or new_records[0].get("id") or "") if new_records else ""
    logger.info("creative_library_ok: action=create key=%s id=%s", key, new_id)
    return new_id or None
