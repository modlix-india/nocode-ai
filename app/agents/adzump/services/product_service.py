"""Product service - saves and restores the user's product and campaign draft.

MySQL (``app.agents.adzump.stores``) is the store of record: ``save_campaign``
writes the typed Product, the new_campaign flow draft and the competitor list
(the adzump_competitors rows are its only home); ``hydrate_from_storage``
restores a returning product from them. The Modlix ``AISuggestedData`` record is a warn-only mirror of the
analysis fields DS still reads (retirement plan S5 deletes it).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any  # noqa: F401  (used in type hints below)

from app.agents.adzump.platform import (
    is_google as _platform_is_google,
    is_meta as _platform_is_meta,
)
from app.agents.adzump.models import CompetitorProfile, OfferState, offer_state
from app.agents.adzump import stores
from app.agents.adzump.models.product import Product, check_product
from app.agents.adzump._shared import (
    STORAGE_CREATE as CREATE,
    STORAGE_READ_PAGE as READ_PAGE,
    STORAGE_UPDATE as UPDATE,
    extract_storage_records as _extract_records,
    normalize_business_url,
    primary_screenshot_url,
    resolve_url,
    storage_headers,
)
from app.agents.appbuilder.tools._shared import get_saas_client

logger = logging.getLogger(__name__)

STORAGE_NAME = "AISuggestedData"
APP_CODE = "marketingai"
SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _storage_headers(ctx: dict) -> dict[str, str]:
    """Auth headers for storage calls. AppCode pinned to ``marketingai``
    because the storage collection is appCode-scoped - clientCode stays
    from the user's session (privacy boundary)."""
    return storage_headers(ctx, APP_CODE)


# ── Reads ─────────────────────────────────────────────────────────────────


async def get_by_url(url: str, ctx: dict) -> dict | None:
    """Return the AISuggestedData record for this URL, or None on miss."""
    if not url:
        return None
    payload = {
        "storageName": STORAGE_NAME,
        "appCode": APP_CODE,
        "clientCode": ctx.get("client_code", ""),
        "filter": {"field": "businessUrl", "value": normalize_business_url(url)},
    }
    result = await get_saas_client().post(
        READ_PAGE, headers=_storage_headers(ctx), json=payload,
    )
    if not result.success:
        logger.info("product_service_read_miss: url=%s err=%s",
                    url, result.error)
        return None
    records = _extract_records(result.data)
    return records[-1] if records else None


# ── Writes ────────────────────────────────────────────────────────────────


async def save_campaign(session_ctx: dict, ctx: dict) -> str | None:
    """Persist the user's campaign (everything assembled in session.context).

    nocode-ai MySQL is the store of record: the typed Product goes to
    adzump_products, the campaign draft to adzump_flows (flow=new_campaign),
    the competitor list to adzump_competitors - a failure there RAISES, the
    save must not silently lose the authoritative copy. The Modlix AISuggestedData
    write survives only as a warn-only mirror of the ANALYSIS fields DS still
    reads (finalSummary, siteLinks, screenshot, location...); the campaign
    sub-object no longer rides in it - no DS code reads it.

    Returns the Modlix record id, or None when the mirror was skipped/failed.
    """
    url = resolve_url(session_ctx)
    if not url:
        logger.warning("save_campaign_skipped: no businessUrl in session")
        return None

    # Schema drift check at the durable boundary - warn-only, never blocks a save.
    check_product(session_ctx.get("product_data") or {}, where="save_campaign")

    # Chat-session provenance: a sub-agent save carries the parent chat id
    # (stamped into shared context by build_sub_session); a direct save uses
    # the tool context's own session id.
    chat_session_id = session_ctx.get("_session_id") or ctx.get("session_id", "")
    record = _build_full_record(session_ctx, url, chat_session_id)
    logger.info(
        "save_campaign_assets: url=%s logo=%s rule=%s conf=%.2f creatives=%d",
        url,
        bool(record.get("logoUrl")),
        (record.get("logoMeta") or {}).get("source") or "",
        float((record.get("logoMeta") or {}).get("confidence") or 0.0),
        len(record.get("creativeImages") or []),
    )

    # Authoritative half: product + campaign draft into nocode-ai MySQL.
    campaign_draft = record.pop("campaign")
    # The SummaryAgent's rich profile lives in product_profile, not
    # product_data - fold it into the persisted Product so resume can show it.
    product = Product.model_validate({
        **(session_ctx.get("product_data") or {}),
        "profile_summary":
            (session_ctx.get("product_profile") or {}).get("summary") or "",
    })
    pid = await stores.products.upsert_product(
        ctx.get("client_code") or "", product, url, ctx.get("user_id") or 0)
    await stores.flows.upsert_flow(
        ctx.get("client_code") or "", pid, chat_session_id, "new_campaign",
        campaign_draft.get("status") or "draft", campaign_draft,
        ctx.get("user_id") or 0)
    await _sync_competitor_list(
        session_ctx, ctx.get("client_code") or "", pid, ctx.get("user_id") or 0)
    record["competitors"] = (session_ctx.get("competitor_analysis") or {}).get(
        "competitors") or []

    return await _mirror_modlix_record(record, url, ctx)


async def _sync_competitor_list(
    session_ctx: dict, client_code: str, product_id: int, user_id: int,
) -> None:
    """Write this chat's competitor list back to its home, the product's rows
    (born 'pending' at save time, so the table holds the list even when the
    user never fetches ads). An entry saved before whose row has since gone
    was deleted elsewhere (the library UI): drop it, never resurrect it."""
    competitive = session_ctx.get("competitor_analysis")
    if competitive is None:
        return  # never loaded or researched in this chat: the rows stand
    stored_ids = {row["id"] for row in
                  await stores.competitors.list_product_competitors(client_code, product_id)}
    entries = [c for c in competitive.get("competitors") or []
               if isinstance(c, dict)
               and (not c.get("row_id") or c["row_id"] in stored_ids)]
    ids = await stores.competitors.sync_competitor_profiles(
        client_code, product_id, entries, user_id)
    for entry, row_id in zip(entries, ids):
        if row_id:
            entry["row_id"] = row_id
    competitive["competitors"] = entries


async def _mirror_modlix_record(record: dict, url: str, ctx: dict) -> str | None:
    """Warn-only mirror of the ANALYSIS fields into Modlix AISuggestedData -
    DS's launch-time consumers (finalSummary, siteLinks, screenshot, ...) read
    it for products DS never scraped. Dies when the last DS consumer moves
    (retirement plan S5). The update is `isPartial` (field-merge), so DS-written
    fields on the same record are never clobbered. Failure only logs - MySQL
    already holds the authoritative copy.

    Payload shapes match ds's `oserver.services.storage_service` - the
    gateway expects `dataObject` / `dataObjectId` / `isPartial`.
    """
    try:
        existing = await get_by_url(url, ctx)

        if existing:
            existing_id = existing.get("_id") or existing.get("id")
            if not existing_id:
                logger.warning(
                    "save_campaign: existing record has no _id, falling back to create")
            else:
                payload = {
                    "storageName": STORAGE_NAME,
                    "appCode": APP_CODE,
                    "dataObjectId": existing_id,
                    "dataObject": record,
                    "isPartial": True,  # merge - preserves existing fields not in record
                }
                result = await get_saas_client().post(
                    UPDATE, headers=_storage_headers(ctx), json=payload,
                )
                if not result.success:
                    logger.warning("save_campaign_update_failed: url=%s err=%s",
                                   url, result.error)
                    return None
                logger.info("save_campaign_ok: action=update url=%s id=%s",
                            url, existing_id)
                return existing_id

        payload = {
            "storageName": STORAGE_NAME,
            "appCode": APP_CODE,
            "dataObject": record,
        }
        result = await get_saas_client().post(
            CREATE, headers=_storage_headers(ctx), json=payload,
        )
        if not result.success:
            logger.warning("save_campaign_create_failed: url=%s err=%s",
                           url, result.error)
            return None

        new_records = _extract_records(result.data)
        new_id = ""
        if new_records:
            new_id = (new_records[0].get("_id") or new_records[0].get("id") or "")
        logger.info("save_campaign_ok: action=create url=%s id=%s", url, new_id)
        return new_id or None
    except Exception as e:
        logger.warning("modlix_mirror_failed: url=%s %s: %s",
                       url, type(e).__name__, str(e)[:200])
        return None


# ── Record construction (pure) ────────────────────────────────────────────


def _build_location_object(spec: dict, product: dict) -> dict:
    """Match the legacy ds-v1 location object shape so ds downstream services
    (chatv2 confirm-location, business_service.update lookup, geo-target
    builders) can keep reading the same keys. Sourced from the confirmed
    product.place, then user-typed spec.
    """
    place = product.get("place") or {}
    coords = (
        {"lng": place.get("lng"), "lat": place.get("lat")}
        if place.get("lat") is not None and place.get("lng") is not None
        else None
    )
    return {
        "area_location": "",
        "product_location": place.get("address") or spec.get("location") or "",
        "product_coordinates": coords,
    }


def _build_map_embeds(product: dict) -> list[dict]:
    """Mirror ds-v1's mapEmbeds entry - empty when we have no coords."""
    place = product.get("place") or {}
    if place.get("lat") is None or place.get("lng") is None:
        return []
    lat = place["lat"]
    lng = place["lng"]
    return [{
        "src": (
            f"https://www.google.com/maps/embed/v1/place?"
            f"q={lat},{lng}&zoom=15"
        ),
        "title": "",
        "coordinates": {"lng": lng, "lat": lat},
    }]


def _build_full_record(session_ctx: dict, url: str, chat_session_id: str = "") -> dict[str, Any]:
    """Build the AISuggestedData record from session.context. Pure function."""
    product = session_ctx.get("product_data") or {}
    profile = session_ctx.get("product_profile") or {}
    spec = session_ctx.get("campaign_spec") or {}
    place = product.get("place") or {}
    account_names = session_ctx.get("account_names") or {}
    competitive = session_ctx.get("competitor_analysis") or {}

    is_meta = _platform_is_meta(spec.get("platform"))

    summary = profile.get("summary") or product.get("summary", "")
    assets = product.get("assets") or {}
    primary_logo = (assets.get("logos") or [{}])[0]
    images = assets.get("images") or []

    return {
        "businessUrl": normalize_business_url(url),

        # ── Analysis fields (mirror ds-v1 schema so its downstream APIs
        #    keep working when reading rows nocode-ai writes) ──
        "summary": summary,
        # ds's google_kw_data_provider reads finalSummary specifically
        "finalSummary": summary,
        "businessType": product.get("business_type", ""),
        "businessScale": product.get("business_scale", "national"),
        # ── Product category (Stage A, taxonomy.py) - the yardstick the
        #    creative-relevance gate judges every competitor ad against ──
        "category": product.get("category", ""),
        "subcategory": product.get("subcategory", ""),
        "market": product.get("market", ""),
        "offeringStage": product.get("offering_stage", ""),
        "categorySource": product.get("category_source", ""),
        "categoryConfidence": float(
            product.get("category_confidence") or 0.0),
        "taxonomyVersion": product.get("taxonomy_version", ""),
        "categoryOverride": product.get("category_override", ""),
        # legacy ds-v1 shape: object with area_location / product_location /
        # product_coordinates. ds chatv2 confirm_location and business_service
        # both read from this dict.
        "location": _build_location_object(spec, product),
        "mapEmbeds": _build_map_embeds(product),
        # `suggestedGeoTargets` and top-level `locations` are deliberately NOT
        # written here. ds resolves them via its geo-target service (Google Ads
        # geoTargetConstants lookup) when needed.
        "screenshot": primary_screenshot_url(product),
        # ds asset services (lead_form, call_assets, whatsapp, site_link)
        # all read siteLinks; default empty so they no-op gracefully
        "siteLinks": product.get("site_links") or [],
        # Product assets - LLM-selected logo + ad-creative-suitable images,
        # already re-hosted on our file service. Consumed by creative-gen.
        # Only the primary logo (logos[0]) is persisted; the stored shape
        # (logoUrl/logoMeta) is the ds-side contract, kept flat here.
        "logoUrl": primary_logo.get("url") or "",
        "logoSourceUrl": primary_logo.get("source_url") or "",
        "logoMeta": {
            "source": primary_logo.get("source") or "",
            "reasoning": primary_logo.get("reasoning") or "",
            "confidence": float(primary_logo.get("confidence") or 0.0),
            # Content-derived render hints (background, fit) - the agent
            # analyzed the image at rehost time and emits these to the UI.
            "display": primary_logo.get("display") or {},
        },
        # The LLM returns as many real images as the site has - no fixed
        # per-page cap. Across multi-page scrapes this can grow; bound the
        # stored record at a high ceiling so a runaway page can't blow the
        # document size. The stored parallel-array shape (creativeImages +
        # creativeDisplays) is the ds-side contract; session shape is
        # assets.images (one object per image).
        "creativeImages": [i.get("url") or "" for i in images][:30],
        "creativeDisplays": [i.get("display") or {} for i in images][:30],
        # Persist scrape budget state so resume hydration can dedupe against
        # what we've already scraped instead of resetting to 0.
        "scrapedUrls": list(product.get("pages") or {}),
        "scrapeCount": len(product.get("pages") or {}),
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
            "sessionId": chat_session_id,
            # Mirror the launch flag, never assert it: the every-turn autosave
            # writes this record too, and a draft stored as "launched" is a
            # consent bypass. launch_campaign sets the flag; any spec edit pops it.
            "status": spec.get("campaign_status") or "draft",
            "platform": spec.get("platform", ""),
            "duration": spec.get("duration", ""),
            "dailyBudget": spec.get("budget", ""),
            "location": {
                "address": place.get("address") or spec.get("location", ""),
                "lat": place.get("lat"),
                "lng": place.get("lng"),
                "displayName": place.get("display_name", ""),
                "country_code": place.get("country_code", ""),
                "country_geo_constant": place.get("country_geo_constant", ""),
            },
            "accounts": {
                "parent": _account_pair(spec.get("parent_account"), account_names),
                "ad": _account_pair(spec.get("account"), account_names),
                "fbPage": _account_pair(spec.get("fb_page"), account_names) if is_meta else None,
                "igPage": _account_pair(spec.get("ig_page"), account_names) if is_meta else None,
            },
            "competitive": {
                "attempted": session_ctx.get("competitor_analysis") is not None,
                # F26 backstop - never persist declined=true alongside attempted
                # (analysis having run voids a prior decline; clear_competitor_decline
                # handles the realistic paths, this keeps the durable record honest
                # even if a stale flag survives an un-instrumented path).
                # Migration-aware read: enum spec and legacy-marker spec answer
                # identically; the ds JSON shape is unchanged (a plain bool).
                "declined": (
                    offer_state(spec, "competitive_analysis") is OfferState.DECLINED
                    and session_ctx.get("competitor_analysis") is None
                ),
            },
            # Persist target areas so they survive session restarts. The
            # per-platform keys are the ds-side contract - projected from
            # target_areas (the handle rides nested per-area).
            "targetAreas": product.get("target_areas") or [],
            "googleMappedLocations": (
                product.get("target_areas") or []
            ) if _platform_is_google(spec.get("platform")) else [],
            "metaMappedLocations": (
                product.get("target_areas") or []
            ) if is_meta else [],
        },
    }


def _account_pair(acct_id: Any, account_names: dict) -> dict | None:
    """Return ``{id, name}`` or None if no id stored."""
    if not acct_id:
        return None
    sid = str(acct_id)
    return {"id": sid, "name": (account_names.get(sid) or "").strip()}


# ── Hydration: MySQL store → session.context ───────────────────────────────


def _apply_hydration(
    session_ctx: dict, product_data: dict, profile: dict,
    location_address: str, competitors: list,
) -> None:
    """Fill the empty session slots from the MySQL hydration source.
    Never overwrites - callers have already done a
    per-session cache check; we only fill empty slots. Restores spec.location
    so the location step skips a fresh confirm_location."""
    if not session_ctx.get("product_data"):
        session_ctx["product_data"] = product_data
        # Schema drift check at the durable boundary - warn-only.
        check_product(product_data, where="hydrate_from_storage")
        session_ctx.setdefault("product_profile", {}).update(profile)
        if location_address:
            session_ctx.setdefault("campaign_spec", {}).setdefault(
                "location", location_address)
        logger.info("hydrate: business loaded url=%s", profile.get("url", ""))
    if not session_ctx.get("competitor_analysis") and competitors:
        session_ctx["competitor_analysis"] = {"competitors": competitors}
        logger.info("hydrate: %d competitors loaded url=%s",
                    len(competitors), profile.get("url", ""))


async def hydrate_from_storage(url: str, session_ctx: dict, ctx: dict) -> bool:
    """Populate session.context (product_data, product_profile, spec.location,
    competitor_analysis) for a returning product. Returns True on hit.

    nocode-ai MySQL is the ONLY hydration source (typed Product round-trip -
    no lossy flat-field rebuild); a miss is a fresh start. The Modlix record
    is a write-only mirror for DS, never read back here. A MySQL ERROR is not
    a miss - it propagates rather than silently starting fresh.
    """
    client_code = ctx.get("client_code") or ""
    key = normalize_business_url(url)
    product = await stores.products.get_product(client_code, key)
    if product is None:
        return False
    pid = await stores.products.product_id(client_code, key)
    draft = await stores.flows.latest_flow(client_code, pid, "new_campaign") or {}
    rows = await stores.competitors.list_product_competitors(client_code, pid)
    ads = await stores.competitors.list_product_creatives(client_code, pid) if rows else {}
    _apply_hydration(
        session_ctx,
        product_data=product.model_dump(),
        profile={"url": key, "title": product.product_name,
                 # Display profile when we have it; the machine brief is the
                 # fallback for records saved before profile_summary existed.
                 "summary": product.profile_summary or product.summary},
        location_address=(draft.get("location") or {}).get("address") or "",
        competitors=[_competitor_entry(row, ads.get(row["id"], [])) for row in rows],
    )
    return True


async def drop_deleted_competitors(competitive: dict, ctx: dict) -> list[str]:
    """Leave out of fresh research results every competitor the user deleted
    from this product - their "no" holds. Returns the names left out."""
    client_code = ctx.get("client_code") or ""
    url = normalize_business_url(resolve_url(ctx.get("session_context") or {}))
    pid = await stores.products.product_id(client_code, url) if url else None
    if pid is None:
        return []
    deleted = await stores.competitors.deleted_competitors(client_code, pid)
    names = {stores.competitors.name_key(row["name"]) for row in deleted}
    urls = {row["url"] for row in deleted if row["url"]}

    def is_deleted(entry: dict) -> bool:
        return (stores.competitors.name_key(entry.get("name") or "") in names
                or stores.competitors.competitor_key(entry.get("url") or "") in urls)

    entries = competitive.get("competitors") or []
    competitive["competitors"] = [c for c in entries if not is_deleted(c)]
    return [c.get("name") or "?" for c in entries if is_deleted(c)]


def _competitor_entry(row: dict, creatives: list) -> dict:
    """A stored competitor row as the session's entry. A fetched row ('ok' or
    'empty') carries its ads; a never-fetched or failed one carries none, so
    the ads offer stays open for it."""
    profile = CompetitorProfile(
        row_id=row["id"], name=row["name"], url=row["url"],
        url_source=row["url_source"] or "", business_type=row["business_type"] or "",
        location=row["location"] or "", pricing=row["pricing"],
        key_usps=row["key_usps"], weakness=row["weakness"],
        why_competitor=row["why_competitor"] or "",
    )
    if row["creative_status"] in ("ok", "empty"):
        profile.creatives = [c.model_dump(by_alias=True) for c in creatives]
        profile.total_creatives = row["total_creatives"]
        profile.active_creatives = row["active_creatives"]
    return profile.to_stored()
