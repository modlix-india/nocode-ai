"""adzump_competitors + adzump_creatives + adzump_creative_assets - one
competitor aggregate: the row, its creatives, one asset row per rendition.

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

from app.db.connection import get_connection
from app.agents.adzump._shared import normalize_business_url
from app.agents.adzump.creative_intelligence.models import Competitor, Creative, Essence
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
    profile, not an ads record, and the fetch lands on it."""
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
                "WHERE c.client_code=%s AND c.creative_status <> 'pending'",
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


# ── Writes ───────────────────────────────────────────────────────────────────


async def sync_competitor_profiles(
    client_code: str, product_id: int, competitors: list[dict], user_id: int = 0,
) -> None:
    """Upsert one PROFILE row per curated competitor, at discovery/curation
    time - so the table always mirrors the analyst's list, fetched or not.
    Touches only profile fields; creative stats and status stay whatever the
    creatives fetch (sync_competitor) last wrote. Rows are born
    creative_status='pending'. url is the competitor's identity (_website - the
    same value sync_competitor writes); VARCHAR(255) fields are clamped so one
    long value can't abort the batch."""
    def clamp(value: str | None) -> str | None:
        return value[:255] if value else None

    async with get_connection() as conn:
        async with conn.cursor() as cur:
            for comp in competitors:
                name = (comp.get("name") or "").strip()[:255]
                if not name:
                    continue
                url = _website(comp.get("url") or "")
                # The curated name wins on a website match: it is what the
                # user reviewed, the vendor's page name never is.
                upsert = """
                    INSERT INTO adzump_competitors
                        (client_code, product_id, name, url, logo_url,
                         location, pricing, created_by, updated_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) AS new
                    ON DUPLICATE KEY UPDATE
                        name=new.name,
                        url=COALESCE(new.url, adzump_competitors.url),
                        logo_url=COALESCE(new.logo_url, adzump_competitors.logo_url),
                        location=COALESCE(new.location, adzump_competitors.location),
                        pricing=COALESCE(new.pricing, adzump_competitors.pricing),
                        updated_by=new.updated_by
                    """
                try:
                    await cur.execute(upsert, (
                        client_code, product_id, name, url,
                        comp.get("logo_url") or None, clamp(comp.get("location")),
                        clamp(comp.get("pricing")), user_id, user_id))
                except pymysql.err.IntegrityError as e:
                    # A rename onto another row's name: skip it, never fail the
                    # every-turn autosave over one entry.
                    logger.warning("sync_competitor_profiles: skipped %r (%s): %s",
                                   name, url, e)
        await conn.commit()


async def sync_competitor(
    client_code: str, product_url: str, competitor: Competitor, user_id: int = 0
) -> int | None:
    """Upsert a competitor and refresh its creatives WHOLESALE for this product.

    One transaction: upsert the adzump_competitors row, delete this competitor's
    creative slice (assets cascade), then re-insert the current creatives + their
    asset rows - latest fetch wins. Returns the competitor row id, or None if the
    product row is missing.
    """
    pid = await products.product_id(client_code, product_url)
    if pid is None:
        logger.warning("sync_competitor: no product row for %s / %s",
                       client_code, product_url)
        return None

    total = len(competitor.creatives)
    active = sum(1 for c in competitor.creatives if c.is_active)
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
                         total_creatives, active_creatives, created_by, updated_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) AS new
                    ON DUPLICATE KEY UPDATE
                        url=COALESCE(new.url, adzump_competitors.url),
                        logo_url=COALESCE(new.logo_url, adzump_competitors.logo_url),
                        location=COALESCE(new.location, adzump_competitors.location),
                        pricing=COALESCE(new.pricing, adzump_competitors.pricing),
                        searched_names=new.searched_names,
                        creatives_fetched_at=new.creatives_fetched_at,
                        creative_status=new.creative_status,
                        fetched_creatives=new.fetched_creatives,
                        dropped_creatives=new.dropped_creatives,
                        total_creatives=new.total_creatives,
                        active_creatives=new.active_creatives,
                        updated_by=new.updated_by
                    """,
                    (client_code, pid, competitor.name, website,
                     competitor.logo_url or None, competitor.location or None,
                     competitor.pricing or None, json.dumps(competitor.searched_names),
                     fetched_at, competitor.fetch_status, fetched, dropped,
                     total, active, user_id, user_id),
                )
                # The row the upsert landed on, found by the same identity.
                if website:
                    await cur.execute(
                        "SELECT id FROM adzump_competitors "
                        "WHERE client_code=%s AND product_id=%s AND url=%s",
                        (client_code, pid, website),
                    )
                else:
                    await cur.execute(
                        "SELECT id FROM adzump_competitors WHERE client_code=%s "
                        "AND product_id=%s AND url IS NULL AND name=%s",
                        (client_code, pid, competitor.name),
                    )
                cid = (await cur.fetchone())[0]

                # Wholesale refresh: drop this competitor's slice, assets cascade.
                await cur.execute(
                    "DELETE FROM adzump_creatives WHERE client_code=%s "
                    "AND source_type='competitor' AND competitor_id=%s AND product_id=%s",
                    (client_code, cid, pid),
                )
                for creative in competitor.creatives:
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
                    essence = (creative.essence.model_dump(by_alias=True)
                               if creative.essence else None)
                    await cur.execute(
                        """
                        INSERT INTO adzump_creative_assets
                            (creative_id, slide_index, aspect_ratio, media_type,
                             file_url, thumbnail_url, width, height,
                             duration_seconds, content_hash, perceptual_hash, essence)
                        VALUES (%s,0,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (creative_row_id, aspect_ratio_bucket(creative.aspect_ratio),
                         "video" if creative.media_type == "video" else "image",
                         creative.file_url or creative.source_asset_url,
                         creative.poster_url or None, creative.width or None,
                         creative.height or None, creative.duration_seconds or None,
                         creative.content_hash or None, creative.perceptual_hash or None,
                         json.dumps(essence) if essence is not None else None),
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
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    return cid


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


def _creative_format(media_type: str) -> str:
    """adzump_creatives.format enum from the Creative media_type. An image ad is a
    'single'; video/carousel/collection map through unchanged."""
    return media_type if media_type in ("video", "carousel", "collection") else "single"


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
    competitor id and reassembled into ``Creative`` objects (one asset per
    creative today: the slide-0 rendition)."""
    if not competitor_ids:
        return {}
    placeholders = ",".join(["%s"] * len(competitor_ids))
    await cur.execute(
        f"SELECT * FROM adzump_creatives WHERE competitor_id IN ({placeholders}) "
        "ORDER BY id",
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
