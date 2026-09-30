"""adzump_competitors + adzump_creatives + adzump_creative_assets - one
competitor aggregate: the row, its creatives, one asset row per rendition.
The rows are the home of a product's competitor list: a chat resumes from
them, and each change a chat makes writes only its own row (add_competitor,
fill_competitor_profile, set_competitor_website, delete_competitor). Nothing
deletes a row but the user's remove.

Rows are scoped to a product: (client_code, product_url), the url normalized
the way adzump_products stores it. Within a product a competitor is identified
by its canonical website (competitor_key), else by its name (name_key) while
no website is known. Binaries live in the Files service; rows carry only urls.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

import aiomysql
import pymysql

from app.db.connection import execute_query, get_connection
from app.agents.adzump._shared import normalize_business_url
from app.agents.adzump.creative_intelligence.models import (
    Competitor, Creative, Essence, Rendition,
)
from app.agents.adzump.stores import products

logger = logging.getLogger(__name__)

_URL_MAX = 512  # adzump_competitors.url VARCHAR(512)

# Standard placement ratios (value = width/height) a competitor creative is
# bucketed into; anything outside the tolerance band lands in 'other'.
_STANDARD_RATIOS: tuple[tuple[str, float], ...] = (
    ("1:1", 1.0), ("4:5", 0.8), ("9:16", 0.5625),
    ("16:9", 1.7778), ("1.91:1", 1.91), ("2:3", 0.6667),
)
_RATIO_TOLERANCE = 0.03


# ── Identity ─────────────────────────────────────────────────────────────────


def competitor_key(url: str) -> str:
    """A competitor's identity within a product: its canonical website - host
    AND path, so project pages on one developer site stay separate competitors
    (Kailash 2026-09-23). Clamped to the column, so the key a read looks up is
    exactly the value both writers store."""
    return normalize_business_url(url)[:_URL_MAX]


def name_key(name: str) -> str:
    """The identity of a competitor with no website yet: ``name:<slug>``."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return f"name:{slug}" if slug else ""


# ── Reads (reassemble a Competitor from rows) ────────────────────────────────


async def get_competitor(
    client_code: str, product_url: str, key: str,
) -> Competitor | None:
    """The stored ``Competitor`` for a key within one product, or None on miss.
    A curated row never fetched ('pending') is a miss: it holds the analyst's
    profile, not an ads record, and the fetch lands on it. A deleted row still
    serves: its ads are the cache that makes re-adding the competitor free."""
    if not key:
        return None
    pid = await products.product_id(client_code, product_url)
    if pid is None:
        return None
    async with get_connection() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            if key.startswith("name:"):
                # Website-less rows are identified by name alone.
                await cur.execute(
                    "SELECT * FROM adzump_competitors "
                    "WHERE client_code=%s AND product_id=%s AND url IS NULL",
                    (client_code, pid),
                )
                row = next((r for r in await cur.fetchall()
                            if name_key(r["name"]) == key), None)
            else:
                await cur.execute(
                    "SELECT * FROM adzump_competitors "
                    "WHERE client_code=%s AND product_id=%s AND url=%s LIMIT 1",
                    (client_code, pid, competitor_key(key)),
                )
                row = await cur.fetchone()
            if not row or row["creative_status"] == "pending":
                return None
            creatives = await _load_creatives(cur, [row["id"]])
    return _row_to_competitor(row, creatives.get(row["id"], []))


async def list_competitors(client_code: str) -> list[tuple[str, Competitor]]:
    """Every fetched competitor record for a client, across its products, as
    (product_url, Competitor) - the repair sweep writes each back to its own
    product. Unparseable rows are skipped, never fatal."""
    async with get_connection() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT c.*, p.url AS product_url FROM adzump_competitors c "
                "JOIN adzump_products p ON p.id = c.product_id "
                "WHERE c.client_code=%s AND c.status='active' "
                "AND c.creative_status <> 'pending'",
                (client_code,),
            )
            rows = await cur.fetchall()
            if not rows:
                return []
            creatives = await _load_creatives(cur, [r["id"] for r in rows])
    records: list[tuple[str, Competitor]] = []
    for row in rows:
        try:
            records.append((row["product_url"],
                            _row_to_competitor(row, creatives.get(row["id"], []))))
        except Exception as e:  # a single bad row must not abort the sweep
            logger.warning("creative_library_reassemble_failed: id=%s err=%s",
                           row.get("id"), str(e)[:150])
    return records


async def list_product_competitors(client_code: str, product_id: int) -> list[dict]:
    """One product's competitor list in the analyst's order, never-fetched
    ('pending') rows included, deleted ones and creatives not."""
    rows = await execute_query(
        "SELECT id, name, url, url_source, logo_url, business_type, location, "
        "pricing, key_usps, weakness, why_competitor, creative_status, "
        "total_creatives, active_creatives, creatives_fetched_at "
        "FROM adzump_competitors WHERE client_code=%s AND product_id=%s "
        "AND status='active' ORDER BY id",
        (client_code, product_id),
    )
    return [{**row, "key_usps": _json_list(row["key_usps"])} for row in rows]


async def list_product_creatives(
    client_code: str, product_id: int, competitor_id: int | None = None,
) -> dict[int, list[Creative]]:
    """One product's competitor creatives grouped by competitor row id,
    optionally narrowed to one competitor. Competitors without ads are absent."""
    sql = ("SELECT id FROM adzump_competitors "
           "WHERE client_code=%s AND product_id=%s AND status='active'")
    params: tuple = (client_code, product_id)
    if competitor_id is not None:
        sql += " AND id=%s"
        params += (competitor_id,)
    async with get_connection() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(sql, params)
            return await _load_creatives(cur, [r["id"] for r in await cur.fetchall()])


# ── Writes ───────────────────────────────────────────────────────────────────


async def deleted_competitors(client_code: str, product_id: int) -> list[dict]:
    """The (name, url) of every competitor the user deleted from a product -
    what research must not suggest again."""
    return await execute_query(
        "SELECT name, url FROM adzump_competitors "
        "WHERE client_code=%s AND product_id=%s AND status='deleted'",
        (client_code, product_id),
    )


async def delete_competitor(
    client_code: str, product_id: int, competitor_id: int, user_id: int = 0,
) -> bool:
    """Mark one active competitor of a product deleted; its row and ads stay.
    False when the product has no such active row for the client."""
    return bool(await execute_query(
        "UPDATE adzump_competitors SET status='deleted', updated_by=%s "
        "WHERE client_code=%s AND product_id=%s AND id=%s AND status='active'",
        (user_id, client_code, product_id, competitor_id),
    ))


async def delete_creative(
    client_code: str, product_id: int, competitor_id: int, creative_id: str,
    user_id: int = 0,
) -> bool:
    """Hide one competitor ad (``creative_id`` is the ad library's id, stable
    across refetches - row ids are not) and recount the competitor's ads.
    False when that competitor has no such active ad for the client."""
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE adzump_creatives SET status='deleted', updated_by=%s "
                "WHERE client_code=%s AND product_id=%s AND competitor_id=%s "
                "AND status='active' AND content->>'$.creativeId'=%s",
                (user_id, client_code, product_id, competitor_id, creative_id))
            hidden = cur.rowcount
            if hidden:
                await _recount_creatives(cur, competitor_id)
        await conn.commit()
    return bool(hidden)


# Insert one competitor, or land on the saved row with its name or website.
# Landing on a saved row:
#   name            never renamed
#   website         the saved one stays
#   profile fields  filled only where empty
#   a deleted row   comes back only with `revive` (the user named it again)
# Returns the active row's id; None when it didn't land (a deleted row, or a
# name and a website that belong to two different saved competitors).
async def add_competitor(
    client_code: str, product_id: int, profile: dict, user_id: int = 0, *, revive: bool,
) -> int | None:
    name = (profile.get("name") or "").strip()[:255]
    if not name:
        return None
    url = _website(profile.get("url") or "")
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(_ADD_COMPETITOR, (
                    client_code, product_id, name, url, profile.get("url_source") or None,
                    _clamp(profile.get("business_type")), _clamp(profile.get("location")),
                    _clamp(profile.get("pricing")), json.dumps(profile.get("key_usps") or []),
                    profile.get("weakness") or None, profile.get("why_competitor") or None,
                    user_id, user_id, revive))
            except pymysql.err.IntegrityError as e:
                logger.warning("add_competitor: %r (%s) is one saved competitor's name "
                               "and another's website: %s", name, url, e)
                return None
            await cur.execute(
                "SELECT id, status FROM adzump_competitors "
                "WHERE client_code=%s AND product_id=%s AND name=%s",
                (client_code, product_id, name))
            row = await cur.fetchone()
            if row is None and url:
                await cur.execute(
                    "SELECT id, status FROM adzump_competitors "
                    "WHERE client_code=%s AND product_id=%s AND url=%s",
                    (client_code, product_id, url))
                row = await cur.fetchone()
        await conn.commit()
    return row[0] if row and row[1] == "active" else None


# url_source is assigned before url: an ON DUPLICATE assignment sees the
# columns updated before it.
_ADD_COMPETITOR = """
    INSERT INTO adzump_competitors
        (client_code, product_id, name, url, url_source, business_type, location,
         pricing, key_usps, weakness, why_competitor, created_by, updated_by)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) AS new
    ON DUPLICATE KEY UPDATE
        status=IF(%s, 'active', adzump_competitors.status),
        url_source=IF(adzump_competitors.url IS NULL, new.url_source,
                      adzump_competitors.url_source),
        url=COALESCE(adzump_competitors.url, new.url),
        business_type=COALESCE(NULLIF(adzump_competitors.business_type, ''), new.business_type),
        location=COALESCE(NULLIF(adzump_competitors.location, ''), new.location),
        pricing=COALESCE(NULLIF(adzump_competitors.pricing, ''), new.pricing),
        key_usps=IF(COALESCE(JSON_LENGTH(adzump_competitors.key_usps), 0) = 0,
                    new.key_usps, adzump_competitors.key_usps),
        weakness=COALESCE(NULLIF(adzump_competitors.weakness, ''), new.weakness),
        why_competitor=COALESCE(NULLIF(adzump_competitors.why_competitor, ''),
                                new.why_competitor),
        updated_by=new.updated_by
"""


# Fill a saved competitor's empty profile fields from a fresh lookup; fields
# already set stay as they are.
async def fill_competitor_profile(
    client_code: str, product_id: int, competitor_id: int, profile: dict, user_id: int = 0,
) -> None:
    await execute_query(
        """
        UPDATE adzump_competitors SET
            business_type=COALESCE(NULLIF(business_type, ''), %s),
            location=COALESCE(NULLIF(location, ''), %s),
            pricing=COALESCE(NULLIF(pricing, ''), %s),
            key_usps=IF(COALESCE(JSON_LENGTH(key_usps), 0) = 0, CAST(%s AS JSON), key_usps),
            weakness=COALESCE(NULLIF(weakness, ''), %s),
            why_competitor=COALESCE(NULLIF(why_competitor, ''), %s),
            updated_by=%s
        WHERE client_code=%s AND product_id=%s AND id=%s
        """,
        (_clamp(profile.get("business_type")), _clamp(profile.get("location")),
         _clamp(profile.get("pricing")), json.dumps(profile.get("key_usps") or []),
         profile.get("weakness") or None, profile.get("why_competitor") or None,
         user_id, client_code, product_id, competitor_id),
    )


# Move a saved competitor to a new website. Its ads stay: the ad search runs on
# the name, never the website (Kailash 2026-09-29).
#   keep_user_pin  a website the user pinned is left alone
# False when nothing moved: another competitor owns that website, the pin held,
# or there is no such row.
async def set_competitor_website(
    client_code: str, product_id: int, competitor_id: int, url: str, url_source: str | None,
    user_id: int = 0, *, keep_user_pin: bool,
) -> bool:
    sql = ("UPDATE adzump_competitors SET url=%s, url_source=%s, updated_by=%s "
           "WHERE client_code=%s AND product_id=%s AND id=%s")
    if keep_user_pin:
        sql += " AND (url_source IS NULL OR url_source <> 'user')"
    try:
        return bool(await execute_query(sql, (
            _website(url), url_source or None, user_id,
            client_code, product_id, competitor_id)))
    except pymysql.err.IntegrityError as e:
        logger.warning("set_competitor_website: %s already belongs to another "
                       "competitor of product %s: %s", url, product_id, e)
        return False


async def sync_competitor(
    client_code: str, product_url: str, competitor: Competitor, user_id: int = 0
) -> int | None:
    """Upsert a competitor and write its record's creatives for this product.

    One transaction: upsert the adzump_competitors row, delete this competitor's
    active creative slice (assets cascade), then re-insert the record's
    creatives + their asset rows. The record carries the stored ads plus the
    fetch's new ones (library._process_stage), so nothing already stored is
    lost. Ads the user hid stay as they are and are never re-inserted. Returns
    the competitor row id, or None if the product row is missing.
    """
    pid = await products.product_id(client_code, product_url)
    if pid is None:
        logger.warning("sync_competitor: no product row for %s / %s",
                       client_code, product_url)
        return None

    total = len(competitor.creatives)
    # fetched = the vendor's raw hit count (library wires it through as
    # fetched_count); dropped = everything fetched that wasn't kept, attribution
    # drops included. Old records without the field fall back to the bounded
    # dropped-trail approximation.
    fetched = competitor.fetched_count or (total + len(competitor.dropped))
    dropped = max(fetched - total, 0)
    fetched_at = competitor.last_fetched_at or None
    website = (None if competitor.competitor_key.startswith("name:")
               else _website(competitor.competitor_key))

    async with get_connection() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO adzump_competitors
                        (client_code, product_id, name, url, logo_url, location,
                         pricing, searched_names, creatives_fetched_at,
                         creative_status, fetched_creatives, dropped_creatives,
                         created_by, updated_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) AS new
                    ON DUPLICATE KEY UPDATE
                        logo_url=COALESCE(new.logo_url, adzump_competitors.logo_url),
                        location=COALESCE(new.location, adzump_competitors.location),
                        pricing=COALESCE(new.pricing, adzump_competitors.pricing),
                        searched_names=new.searched_names,
                        creatives_fetched_at=new.creatives_fetched_at,
                        creative_status=new.creative_status,
                        fetched_creatives=new.fetched_creatives,
                        dropped_creatives=new.dropped_creatives,
                        updated_by=new.updated_by
                    """,
                    (client_code, pid, competitor.name, website,
                     competitor.logo_url or None, competitor.location or None,
                     competitor.pricing or None, json.dumps(competitor.searched_names),
                     fetched_at, competitor.fetch_status, fetched, dropped,
                     user_id, user_id),
                )
                # The row the upsert landed on: its name, else its website. The
                # ads write never moves a website (a stale chat's fetch would
                # undo another chat's pin), so a name-keyed fetch can meet a row
                # that has one.
                await cur.execute(
                    "SELECT id FROM adzump_competitors "
                    "WHERE client_code=%s AND product_id=%s AND name=%s",
                    (client_code, pid, competitor.name),
                )
                row = await cur.fetchone()
                if row is None and website:
                    await cur.execute(
                        "SELECT id FROM adzump_competitors "
                        "WHERE client_code=%s AND product_id=%s AND url=%s",
                        (client_code, pid, website),
                    )
                    row = await cur.fetchone()
                cid = row[0]

                # Wholesale refresh of the ACTIVE slice, assets cascade; the
                # user's hidden ads are kept and skipped.
                await cur.execute(
                    "SELECT content->>'$.creativeId' FROM adzump_creatives "
                    "WHERE competitor_id=%s AND status='deleted'", (cid,))
                hidden = {row[0] for row in await cur.fetchall()}
                await cur.execute(
                    "DELETE FROM adzump_creatives WHERE client_code=%s "
                    "AND source_type='competitor' AND competitor_id=%s "
                    "AND product_id=%s AND status='active'",
                    (client_code, cid, pid),
                )
                for creative in competitor.creatives:
                    if creative.creative_id in hidden:
                        continue
                    await cur.execute(
                        """
                        INSERT INTO adzump_creatives
                            (client_code, competitor_id, product_id, format,
                             source_type, is_public, is_active, days_running,
                             content, created_by, updated_by)
                        VALUES (%s,%s,%s,%s,'competitor',0,%s,%s,%s,%s,%s)
                        """,
                        (client_code, cid, pid, _creative_format(creative.media_type),
                         creative.is_active, creative.days_running,
                         json.dumps(_creative_content(creative)), user_id, user_id),
                    )
                    creative_row_id = cur.lastrowid
                    asset = _primary_asset(creative)
                    await cur.execute(
                        f"INSERT INTO adzump_creative_assets "
                        f"(creative_id, slide_index, {', '.join(asset)}) "
                        f"VALUES (%s,0,{','.join(['%s'] * len(asset))})",
                        (creative_row_id, *asset.values()),
                    )
                    # Placement versions: one asset row per rendition ratio
                    # (grouping guarantees the buckets are distinct - the
                    # (creative_id, slide_index, aspect_ratio) key holds).
                    for rendition in creative.renditions:
                        await cur.execute(
                            """
                            INSERT INTO adzump_creative_assets
                                (creative_id, slide_index, aspect_ratio,
                                 media_type, file_url, width, height,
                                 content_hash, perceptual_hash)
                            VALUES (%s,0,%s,'image',%s,%s,%s,%s,%s)
                            """,
                            (creative_row_id,
                             aspect_ratio_bucket(rendition.aspect_ratio),
                             rendition.file_url, rendition.width or None,
                             rendition.height or None,
                             rendition.content_hash or None,
                             rendition.perceptual_hash or None),
                        )
                await _recount_creatives(cur, cid)
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    return cid


async def _recount_creatives(cur, competitor_id: int) -> None:
    """The competitor's ad counts, recounted from its active (not hidden) ads."""
    await cur.execute(
        "UPDATE adzump_competitors SET "
        "total_creatives=(SELECT COUNT(*) FROM adzump_creatives "
        "WHERE competitor_id=%s AND status='active'), "
        "active_creatives=(SELECT COUNT(*) FROM adzump_creatives "
        "WHERE competitor_id=%s AND status='active' AND is_active=1) "
        "WHERE id=%s",
        (competitor_id, competitor_id, competitor_id))


def aspect_ratio_bucket(ratio: float) -> str:
    """Snap a raw width/height ratio to a standard placement ratio, or 'other'.
    A competitor ad arrives at any dimensions; width/height keep the real value."""
    for name, value in _STANDARD_RATIOS:
        if abs(ratio - value) <= _RATIO_TOLERANCE:
            return name
    return "other"


# ── Row <-> model mapping (pure) ─────────────────────────────────────────────


def _website(url: str) -> str | None:
    """The url column value: the competitor key, NULL while none is known."""
    return competitor_key(url) or None


# A VARCHAR(255) profile field cut to fit, so one long value can't fail a write.
def _clamp(value: str | None) -> str | None:
    return value[:255] if value else None


def _creative_format(media_type: str) -> str:
    """adzump_creatives.format enum from the Creative media_type. An image ad is a
    'single'; video/carousel/collection map through unchanged."""
    return media_type if media_type in ("video", "carousel", "collection") else "single"


def _primary_asset(creative: Creative) -> dict:
    """The slide-0 adzump_creative_assets columns for a creative - the one place
    the writer maps them, so _row_to_creative is tested against the same shape."""
    essence = creative.essence.model_dump(by_alias=True) if creative.essence else None
    return {
        "aspect_ratio": aspect_ratio_bucket(creative.aspect_ratio),
        "media_type": "video" if creative.media_type == "video" else "image",
        "file_url": creative.file_url or creative.source_asset_url,
        "thumbnail_url": creative.poster_url or None,
        "width": creative.width or None,
        "height": creative.height or None,
        "duration_seconds": creative.duration_seconds or None,
        "content_hash": creative.content_hash or None,
        "perceptual_hash": creative.perceptual_hash or None,
        "essence": json.dumps(essence) if essence is not None else None,
    }


def _creative_content(creative: Creative) -> dict:
    """Ad-level metadata that is not a promoted column - the creatives.content blob.
    Everything binary/per-slide goes to the asset row instead."""
    return {
        "creativeId": creative.creative_id,
        "headline": creative.headline,
        "primaryText": creative.primary_text,
        "description": creative.description,
        "cta": creative.cta,
        "landingUrl": creative.landing_url,
        "platform": creative.platform,
        "format": creative.format,
        "publisherPlatforms": creative.publisher_platforms,
        "firstSeen": creative.first_seen,
        "lastSeen": creative.last_seen,
        "verifiedAt": creative.verified_at,
        "winnerSignal": creative.winner_signal,
        "variants": creative.variants,
        "renditions": [r.model_dump(by_alias=True) for r in creative.renditions],
    }


async def _load_creatives(cur, competitor_ids: list[int]) -> dict[int, list[Creative]]:
    """Load creatives + their assets for the given competitors, grouped by
    competitor id and reassembled into ``Creative`` objects: the first asset is
    the primary rendition, the placement renditions come back from content."""
    if not competitor_ids:
        return {}
    placeholders = ",".join(["%s"] * len(competitor_ids))
    await cur.execute(
        f"SELECT * FROM adzump_creatives WHERE competitor_id IN ({placeholders}) "
        "AND status='active' ORDER BY id",
        tuple(competitor_ids),
    )
    creative_rows = await cur.fetchall()
    if not creative_rows:
        return {}
    ids = [r["id"] for r in creative_rows]
    ph = ",".join(["%s"] * len(ids))
    await cur.execute(
        f"SELECT * FROM adzump_creative_assets WHERE creative_id IN ({ph}) "
        "ORDER BY creative_id, slide_index, id",
        tuple(ids),
    )
    assets_by_creative: dict[int, dict] = {}
    for a in await cur.fetchall():
        assets_by_creative.setdefault(a["creative_id"], a)  # first = slide 0

    grouped: dict[int, list[Creative]] = {}
    for cr in creative_rows:
        creative = _row_to_creative(cr, assets_by_creative.get(cr["id"]))
        grouped.setdefault(cr["competitor_id"], []).append(creative)
    return grouped


def _row_to_competitor(row: dict, creatives: list[Creative]) -> Competitor:
    return Competitor(
        competitorKey=row["url"] or name_key(row["name"]),
        name=row["name"],
        logoUrl=row["logo_url"] or "",
        location=row["location"] or "",
        pricing=row["pricing"] or "",
        searchedNames=_json_list(row["searched_names"]),
        lastFetchedAt=_iso(row["creatives_fetched_at"]),
        fetchStatus=row["creative_status"],
        creatives=creatives,
    )


def _row_to_creative(cr: dict, asset: dict | None) -> Creative:
    content = _json_obj(cr["content"])
    fmt = cr["format"]
    media_type = "image" if fmt == "single" else fmt
    essence = None
    width = height = 0
    file_url = poster_url = content_hash = perceptual_hash = ""
    duration = 0.0
    if asset:
        width = asset["width"] or 0
        height = asset["height"] or 0
        file_url = asset["file_url"] or ""
        poster_url = asset["thumbnail_url"] or ""
        content_hash = asset["content_hash"] or ""
        perceptual_hash = asset["perceptual_hash"] or ""
        duration = asset["duration_seconds"] or 0.0
        if asset["essence"]:
            essence = Essence.model_validate(_json_obj(asset["essence"]))
    return Creative(
        creativeId=content.get("creativeId", ""),
        mediaType=media_type,
        fileUrl=file_url,
        posterUrl=poster_url,
        contentHash=content_hash,
        perceptualHash=perceptual_hash,
        headline=content.get("headline", ""),
        primaryText=content.get("primaryText", ""),
        description=content.get("description", ""),
        cta=content.get("cta", ""),
        landingUrl=content.get("landingUrl", ""),
        platform=content.get("platform", ""),
        format=content.get("format", ""),
        publisherPlatforms=content.get("publisherPlatforms", []),
        firstSeen=content.get("firstSeen", ""),
        lastSeen=content.get("lastSeen", ""),
        verifiedAt=content.get("verifiedAt", ""),
        isActive=bool(cr["is_active"]),
        daysRunning=cr["days_running"] or 0,
        variants=content.get("variants", 0),
        width=width,
        height=height,
        aspectRatio=(width / height) if height else 0.0,
        durationSeconds=duration,
        essence=essence,
        renditions=[Rendition.model_validate(r) for r in content.get("renditions", [])],
    )


def _iso(ts) -> str:
    return ts.isoformat() if isinstance(ts, datetime) else (ts or "")


def _json_obj(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    return json.loads(raw) if raw else {}


def _json_list(raw) -> list:
    if isinstance(raw, list):
        return raw
    return json.loads(raw) if raw else []
