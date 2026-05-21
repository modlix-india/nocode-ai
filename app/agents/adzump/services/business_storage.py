"""Persist business + campaign data to nocode-saas storage.

Single record per businessUrl in the shared `AISuggestedData` collection
(appCode `marketingai`). Adds a `campaign` sub-object alongside the existing
analysis fields. Latest-launch-wins per URL — no history (yet).

Reuses:
- `app.agents.appbuilder.tools._shared.get_saas_client` (shared SaasClient singleton)
- `app.agents.adzump.tools._shared.build_ds_headers` (auth headers from tool context)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any  # noqa: F401  (used in type hints below)
from urllib.parse import urlparse

from app.agents.adzump.platform import is_meta as _platform_is_meta
from app.agents.adzump.tools._shared import build_ds_headers
from app.agents.appbuilder.tools._shared import get_saas_client

from app.agents.adzump.agents.product.models import StorageReadRequest


logger = logging.getLogger(__name__)

STORAGE_NAME = "AISuggestedData"
APP_CODE = "marketingai"
SCHEMA_VERSION = 1

READ_PAGE = "/api/core/function/execute/CoreServices.Storage/ReadPage"
CREATE = "/api/core/function/execute/CoreServices.Storage/Create"
UPDATE = "/api/core/function/execute/CoreServices.Storage/Update"


def _normalize_url(url: str) -> str:
    """Canonicalize a business URL for storage keys and lookups.

    - Force ``https`` scheme so the same business doesn't end up with two
      records keyed under ``http://`` and ``https://``.
    - Lowercase host, strip leading ``www.``, drop trailing slash.
    """
    if not url:
        return ""
    p = urlparse(url.strip())
    host = (p.netloc or "").lower().removeprefix("www.")
    path = (p.path or "").rstrip("/")
    return f"https://{host}{path}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _storage_headers(ctx: dict) -> dict[str, str]:
    """Auth headers for storage calls. AppCode pinned to ``marketingai``
    because the storage collection is appCode-scoped — clientCode stays
    from the user's session (privacy boundary)."""
    h = build_ds_headers(ctx)
    h["AppCode"] = APP_CODE
    h["Content-Type"] = "application/json"
    return h


# ── Reads ─────────────────────────────────────────────────────────────────


async def storage_read(request: StorageReadRequest, ctx: dict) -> list[dict]:
    """Execute a storage read using the provided request model and return extracted records."""
    # Mirror the POST pattern from other repos while maintaining compatibility with get_saas_client
    result = await get_saas_client().post(
        READ_PAGE,
        headers=_storage_headers(ctx),
        json=request.model_dump(exclude_none=True),
    )

    if not result.success:
        logger.warning("storage_read_failed: err=%s", result.error)
        return []

    return _extract_records(result.data)


def _extract_records(raw: Any) -> list[dict]:
    """Mirror ds's StorageResponse.content unwrap: gateway wraps the storage
    result in two `result` levels, then either has `content` (paged) or
    returns records directly. Tolerates both."""
    if raw is None:
        return []
    data = raw
    if isinstance(data, list) and data:
        data = data[0]
    # 2-level unwrap of the known `result.result` envelope
    for _ in range(2):
        if isinstance(data, dict) and "result" in data:
            data = data["result"]
        else:
            break
    if data is None:
        return []
    if isinstance(data, dict) and "content" in data:
        content = data["content"]
        return content if isinstance(content, list) else [content]
    return data if isinstance(data, list) else [data]


async def get_by_url(url: str, ctx: dict) -> dict | None:
    """Return the AISuggestedData record for this URL, or None on miss."""
    if not url:
        return None
    payload = {
        "storageName": STORAGE_NAME,
        "appCode": APP_CODE,
        "clientCode": ctx.get("client_code", ""),
        "filter": {"field": "businessUrl", "value": _normalize_url(url)},
    }
    result = await get_saas_client().post(
        READ_PAGE,
        headers=_storage_headers(ctx),
        json=payload,
    )
    if not result.success:
        logger.info("business_storage_read_miss: url=%s err=%s", url, result.error)
        return None
    records = _extract_records(result.data)
    return records[-1] if records else None


# ── Writes ────────────────────────────────────────────────────────────────


async def save_campaign(session_ctx: dict, ctx: dict) -> str | None:
    """Persist the user's campaign (everything assembled in session.context).

    Writes to the same per-URL record `ds/chatv2` uses, alongside the
    business-analysis fields. The `campaign` sub-object is set/overwritten
    on each launch. Returns the storage record id (existing or new), or
    None on failure.

    Payload shapes match ds's `oserver.services.storage_service` — the
    gateway expects `dataObject` / `dataObjectId` / `isPartial`.
    """
    url = resolve_url(session_ctx)
    if not url:
        logger.warning("save_campaign_skipped: no businessUrl in session")
        return None

    record = _build_full_record(session_ctx, url)
    existing = await get_by_url(url, ctx)

    if existing:
        existing_id = existing.get("_id") or existing.get("id")
        if not existing_id:
            logger.warning(
                "save_campaign: existing record has no _id, falling back to create"
            )
        else:
            payload = {
                "storageName": STORAGE_NAME,
                "appCode": APP_CODE,
                "dataObjectId": existing_id,
                "dataObject": record,
                "isPartial": True,  # merge — preserves existing fields not in record
            }
            result = await get_saas_client().post(
                UPDATE,
                headers=_storage_headers(ctx),
                json=payload,
            )
            if not result.success:
                logger.warning(
                    "save_campaign_update_failed: url=%s err=%s", url, result.error
                )
                return None
            logger.info(
                "save_campaign_ok: action=update url=%s id=%s", url, existing_id
            )
            return existing_id

    # Create path
    payload = {
        "storageName": STORAGE_NAME,
        "appCode": APP_CODE,
        "dataObject": record,
    }
    result = await get_saas_client().post(
        CREATE,
        headers=_storage_headers(ctx),
        json=payload,
    )
    if not result.success:
        logger.warning("save_campaign_create_failed: url=%s err=%s", url, result.error)
        return None

    new_records = _extract_records(result.data)
    new_id = ""
    if new_records:
        new_id = new_records[0].get("_id") or new_records[0].get("id") or ""
    logger.info("save_campaign_ok: action=create url=%s id=%s", url, new_id)
    return new_id or None


# ── Record construction (pure) ────────────────────────────────────────────


def resolve_url(session_ctx: dict) -> str:
    """Find the business URL across the various places it can live."""
    profile = session_ctx.get("product_profile") or {}
    if profile.get("url"):
        return profile["url"]
    product = session_ctx.get("product_data") or {}
    pages = product.get("pages_analyzed") or []
    if pages:
        return pages[0]
    return ""


def _build_location_object(loc_meta: dict, spec: dict, product: dict) -> dict:
    """Match the legacy ds-v1 location object shape so ds downstream services
    (chatv2 confirm-location, business_service.update lookup, geo-target
    builders) can keep reading the same keys. Map-confirmed location wins,
    then user-typed spec, then scraped-from-website.
    """
    coords = (
        {"lng": loc_meta.get("lng"), "lat": loc_meta.get("lat")}
        if loc_meta.get("lat") is not None and loc_meta.get("lng") is not None
        else None
    )
    return {
        "area_location": "",
        "product_location": (
            loc_meta.get("address")
            or spec.get("location")
            or product.get("location", "")
        ),
        "product_coordinates": coords,
    }


def _build_map_embeds(loc_meta: dict) -> list[dict]:
    """Mirror ds-v1's mapEmbeds entry — empty when we have no coords."""
    if loc_meta.get("lat") is None or loc_meta.get("lng") is None:
        return []
    lat = loc_meta["lat"]
    lng = loc_meta["lng"]
    return [
        {
            "src": (
                f"https://www.google.com/maps/embed/v1/place?q={lat},{lng}&zoom=15"
            ),
            "title": "",
            "coordinates": {"lng": lng, "lat": lat},
        }
    ]


def _build_full_record(session_ctx: dict, url: str) -> dict[str, Any]:
    """Build the AISuggestedData record from session.context. Pure function."""
    product = session_ctx.get("product_data") or {}
    profile = session_ctx.get("product_profile") or {}
    spec = session_ctx.get("campaign_spec") or {}
    loc_meta = session_ctx.get("_location_meta") or {}
    account_names = session_ctx.get("account_names") or {}
    competitive = session_ctx.get("competitor_analysis") or {}

    is_meta = _platform_is_meta(spec.get("platform"))

    summary = profile.get("summary") or product.get("summary", "")

    return {
        "businessUrl": _normalize_url(url),
        # ── Analysis fields (mirror ds-v1 schema so its downstream APIs
        #    keep working when reading rows nocode-ai writes) ──
        "summary": summary,
        # ds's google_kw_data_provider reads finalSummary specifically
        "finalSummary": summary,
        "businessType": product.get("business_type", ""),
        # legacy ds-v1 shape: object with area_location / product_location /
        # product_coordinates. ds chatv2 confirm_location and business_service
        # both read from this dict.
        "location": _build_location_object(loc_meta, spec, product),
        "mapEmbeds": _build_map_embeds(loc_meta),
        # `suggestedGeoTargets` and top-level `locations` are deliberately NOT
        # written here. ds resolves them via its geo-target service (Google Ads
        # geoTargetConstants lookup) when needed. The LLM's free-text guesses
        # in product.suggested_locations are unreliable and would shadow the
        # real resolution.
        "screenshot": (
            product.get("primary_screenshot_url")
            or product.get("screenshot_url")
            or product.get("screenshot")
            or ""
        ),
        "externalLinks": product.get("external_links") or [],
        # ds asset services (lead_form, call_assets, whatsapp, site_link)
        # all read siteLinks; default empty so they no-op gracefully
        "siteLinks": product.get("site_links") or [],
        # ds-v1 writes/reads `businessName`; nocode-ai's analyst calls it
        # `productName`. Mirror both so ds APIs (chatv2/confirm.py,
        # third_party/google/.../build_google_search_ad_payload.py,
        # tools/account_selection_tool.py, etc.) keep working.
        "productName": product.get("product_name", ""),
        "businessName": product.get("product_name", ""),
        "uniqueFeatures": product.get("unique_features") or [],
        "productsServices": product.get("products_services") or [],
        "pricing": product.get("pricing", ""),
        "contact": product.get("contact") or {},
        "pagesAnalyzed": product.get("pages_analyzed") or [],
        "competitors": (competitive or {}).get("competitors") or [],
        # ── Provenance ──
        "lastAnalyzedAt": _now_iso(),
        "lastAnalyzedBy": "adzump-launch",
        "schemaVersion": SCHEMA_VERSION,
        # ── Campaign sub-object ──
        "campaign": {
            "savedAt": _now_iso(),
            "sessionId": session_ctx.get("_session_id", ""),
            "status": "launched",
            "platform": spec.get("platform", ""),
            "duration": spec.get("duration", ""),
            "dailyBudget": spec.get("budget", ""),
            "location": {
                "address": loc_meta.get("address") or spec.get("location", ""),
                "lat": loc_meta.get("lat"),
                "lng": loc_meta.get("lng"),
                "displayName": loc_meta.get("displayName", ""),
            },
            "accounts": {
                "parent": _account_pair(spec.get("parent_account"), account_names),
                "ad": _account_pair(spec.get("account"), account_names),
                "fbPage": _account_pair(spec.get("fb_page"), account_names)
                if is_meta
                else None,
                "igPage": _account_pair(spec.get("ig_page"), account_names)
                if is_meta
                else None,
            },
            "competitive": {
                "attempted": session_ctx.get("competitor_analysis") is not None,
                "declined": spec.get("competitive_analysis_declined") == "true",
            },
        },
    }


def _account_pair(acct_id: Any, account_names: dict) -> dict | None:
    """Return ``{id, name}`` or None if no id stored."""
    if not acct_id:
        return None
    sid = str(acct_id)
    return {"id": sid, "name": (account_names.get(sid) or "").strip()}


# ── Hydration: storage record → session.context ───────────────────────────


def _record_data(record: dict) -> dict:
    """Storage records nest the writable fields under `data`. Some readers
    return them flat. Tolerate both shapes."""
    if isinstance(record.get("data"), dict):
        return record["data"]
    return record


def _record_to_business(record: dict) -> dict:
    """Translate AISuggestedData (camelCase) → adzump's product_data shape."""
    d = _record_data(record)
    screenshot = d.get("screenshot", "")
    # `location` was a plain string historically; nocode-ai now writes the
    # ds-v1 object shape `{area_location, product_location, product_coordinates}`.
    # Hydrate to a string for product_data.location consumers.
    raw_loc = d.get("location") or ""
    if isinstance(raw_loc, dict):
        location_str = (
            raw_loc.get("product_location") or raw_loc.get("area_location") or ""
        )
    else:
        location_str = raw_loc
    return {
        # Tolerate ds-v1 records that only set `businessName` (we now write
        # both — see _build_full_record).
        "product_name": d.get("productName") or d.get("businessName", ""),
        "business_type": d.get("businessType", ""),
        "summary": d.get("summary", ""),
        "location": location_str,
        "suggested_locations": d.get("suggestedGeoTargets") or [],
        # Populate both keys so downstream consumers (craft renderers,
        # competitor.py's `_emit_final_craft` etc.) find a screenshot under
        # whichever name they expect.
        "primary_screenshot_url": screenshot,
        "screenshot_url": screenshot,
        "external_links": d.get("externalLinks") or [],
        "unique_features": d.get("uniqueFeatures") or [],
        "products_services": d.get("productsServices") or [],
        "pricing": d.get("pricing", ""),
        "contact": d.get("contact") or {},
        "pages_analyzed": d.get("pagesAnalyzed") or [],
    }


def _record_to_competitive(record: dict) -> dict | None:
    """Pull the competitors array out of the record. Returns None if empty —
    so callers can distinguish 'never analyzed' from 'analyzed but empty'."""
    d = _record_data(record)
    competitors = d.get("competitors") or []
    if not competitors:
        return None
    return {"competitors": competitors}


async def hydrate_from_storage(url: str, session_ctx: dict, ctx: dict) -> bool:
    """Look up the AISuggestedData record for `url`. If found, populate
    session.context with product_data, product_profile, and (if present)
    competitor_analysis. Returns True on hit, False on miss.

    Skips overwrite when session already has the field — the caller has
    already done a per-session cache check; we only fill empty slots.
    """
    record = await get_by_url(url, ctx)
    if not record:
        return False

    if not session_ctx.get("product_data"):
        session_ctx["product_data"] = _record_to_business(record)
        d = _record_data(record)
        session_ctx.setdefault("product_profile", {}).update(
            {
                "url": d.get("businessUrl") or url,
                # Tolerate ds-v1 records that only set `businessName`.
                "title": d.get("productName") or d.get("businessName", ""),
                "summary": d.get("summary", ""),
            }
        )
        logger.info("hydrate_from_storage: business loaded url=%s", url)

    if not session_ctx.get("competitor_analysis"):
        comp = _record_to_competitive(record)
        if comp:
            session_ctx["competitor_analysis"] = comp
            logger.info(
                "hydrate_from_storage: %d competitors loaded url=%s",
                len(comp.get("competitors") or []),
                url,
            )

    return True


# ── Lat/lng capture from map widget callback ──────────────────────────────


_LOCATION_UPDATE_RE = re.compile(r'"type"\s*:\s*"location_update"')


def parse_location_update(user_message: str) -> dict | None:
    """If the user's message is a `location_update` JSON callback, return
    ``{address, lat, lng}``. Returns None for plain "confirm" or anything
    else.
    """
    if not user_message:
        return None
    msg = user_message.strip()
    if not msg.startswith("{") or not _LOCATION_UPDATE_RE.search(msg):
        return None
    try:
        payload = json.loads(msg)
    except (ValueError, json.JSONDecodeError):
        return None
    if payload.get("type") != "location_update":
        return None
    return {
        "address": (payload.get("address") or "").strip(),
        "lat": payload.get("lat"),
        "lng": payload.get("lng"),
    }
