"""SQL DAL for the adzump store (migration V17).

Five tables, one module: adzump_products, adzump_flows, adzump_competitors,
adzump_creatives, adzump_creative_assets. Raw SQL over the shared aiomysql pool
(app.db.connection), same as the ai_tracking / lore tables.

`data` (products) and `content` (creatives) are the typed source of truth; the
promoted columns are projections written from the same object in the same
statement, so they can never drift from the JSON.
"""

from __future__ import annotations

import json
import logging

import aiomysql

from app.db.connection import get_connection
from app.agents.adzump.models.product import Product
from app.agents.adzump.creative_intelligence.models import Competitor, Creative
from app.agents.adzump.services.business_storage import (
    normalize_business_url,
    resolve_url,
)

logger = logging.getLogger(__name__)


def scope(ctx: dict) -> tuple[str, str]:
    """The (client_code, product_url) a creative-store call is scoped to, both
    derived from ctx. product_url is normalized the SAME way products are stored,
    so the competitor/creative FK lookups always match adzump_products.url."""
    client_code = ctx.get("client_code") or ""
    product_url = normalize_business_url(resolve_url(ctx.get("session_context") or {}))
    return client_code, product_url

# Standard placement ratios (value = width/height) a competitor creative is
# bucketed into; anything outside the tolerance band lands in 'other'.
_STANDARD_RATIOS: tuple[tuple[str, float], ...] = (
    ("1:1", 1.0), ("4:5", 0.8), ("9:16", 0.5625),
    ("16:9", 1.7778), ("1.91:1", 1.91), ("2:3", 0.6667),
)
_RATIO_TOLERANCE = 0.03


# ── Products ─────────────────────────────────────────────────────────────────


def _project(product: Product) -> dict:
    """The queried scalars lifted out of the typed Product. The ONLY place
    columns are derived from the object, so `data` and the columns agree.
    `url` is NOT here - it is the resolved business url the caller passes, so
    the key always matches scope()/normalize_business_url."""
    return {
        "name": product.product_name,
        "scale": product.business_scale,
        "category": product.category,
        "country_code": product.place.country_code,
        "summary": product.summary,
    }


async def upsert_product(
    client_code: str, product: Product, url: str, user_id: int = 0
) -> int:
    """Insert or update the product row keyed by (client_code, url). `url` is the
    resolved+normalized business url (the storage key, same as scope()), NOT
    product.primary_url which can diverge. The typed Product travels in `data`."""
    url = normalize_business_url(url)
    if not url:
        raise ValueError("upsert_product: empty business url")
    cols = _project(product)
    data = json.dumps(product.model_dump(mode="json"))
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO adzump_products
                    (client_code, url, name, scale, category, country_code,
                     summary, data, created_by, updated_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) AS new
                ON DUPLICATE KEY UPDATE
                    name=new.name, scale=new.scale,
                    category=new.category, country_code=new.country_code,
                    summary=new.summary, data=new.data,
                    updated_by=new.updated_by
                """,
                (client_code, url, cols["name"], cols["scale"],
                 cols["category"], cols["country_code"], cols["summary"],
                 data, user_id, user_id),
            )
            await conn.commit()
    return await product_id(client_code, url)


async def get_product(client_code: str, url: str) -> Product | None:
    """Hydrate the typed Product back from `data`, or None on miss."""
    async with get_connection() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT data FROM adzump_products WHERE client_code=%s AND url=%s",
                (client_code, url),
            )
            row = await cur.fetchone()
    if not row:
        return None
    raw = row["data"]
    return Product.model_validate(raw if isinstance(raw, dict) else json.loads(raw))


async def product_id(client_code: str, url: str) -> int | None:
    """The row id for a (client, url), or None if absent - the FK the competitor
    and creative writers resolve against."""
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM adzump_products WHERE client_code=%s AND url=%s",
                (client_code, url),
            )
            row = await cur.fetchone()
    return row[0] if row else None


# ── Flows ─────────────────────────────────────────────────────────────────────
# Universal per-flow session state: any flow persists its accumulated `data`
# and resumes from the latest row. Entities the flow produces live in their
# own typed tables; flow POSITION is never stored (the journey engine
# recomputes it from the data).


async def upsert_flow(
    client_code: str, product_id: int, session_id: str, flow: str,
    status: str, data: dict, user_id: int = 0,
) -> None:
    """Insert or update the flow-state row for (client, product, chat
    session, flow). Several runs per product are allowed; resume reads the
    most recently updated one (latest_flow)."""
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO adzump_flows
                    (client_code, product_id, session_id, flow, status,
                     data, created_by, updated_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) AS new
                ON DUPLICATE KEY UPDATE
                    status=new.status, data=new.data, updated_by=new.updated_by
                """,
                (client_code, product_id, session_id or "", flow,
                 status or "draft", json.dumps(data), user_id, user_id),
            )
            await conn.commit()


async def latest_flow(
    client_code: str, product_id: int | None, flow: str,
) -> dict | None:
    """The most recently updated state for (product, flow) - what resume
    hydrates from - or None when no run has been persisted yet."""
    if product_id is None:
        return None
    async with get_connection() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT data FROM adzump_flows "
                "WHERE client_code=%s AND product_id=%s AND flow=%s "
                "ORDER BY updated_at DESC, id DESC LIMIT 1",
                (client_code, product_id, flow),
            )
            row = await cur.fetchone()
    if not row:
        return None
    raw = row["data"]
    return raw if isinstance(raw, dict) else json.loads(raw)


async def sync_competitor_profiles(
    client_code: str, product_id: int, competitors: list[dict], user_id: int = 0,
) -> None:
    """Upsert one adzump_competitors PROFILE row per curated competitor, at
    discovery/curation time - so the table always mirrors the analyst's list,
    fetched or not. Touches only profile fields; creative stats and status
    stay whatever the creatives fetch (sync_competitor) last wrote. Rows are
    born creative_status='pending'. url is stored normalized (https host+path,
    no query/fragment - analysts hand back ad click-through urls full of
    tracking params); VARCHAR(255) fields are clamped so one long value can't
    abort the batch."""
    def clamp(value: str | None) -> str | None:
        return value[:255] if value else None

    async with get_connection() as conn:
        async with conn.cursor() as cur:
            for comp in competitors:
                name = (comp.get("name") or "").strip()[:255]
                if not name:
                    continue
                url = normalize_business_url(comp.get("url") or "") or None
                await cur.execute(
                    """
                    INSERT INTO adzump_competitors
                        (client_code, product_id, name, url, logo_url,
                         location, pricing, created_by, updated_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) AS new
                    ON DUPLICATE KEY UPDATE
                        url=COALESCE(new.url, adzump_competitors.url),
                        logo_url=COALESCE(new.logo_url, adzump_competitors.logo_url),
                        location=COALESCE(new.location, adzump_competitors.location),
                        pricing=COALESCE(new.pricing, adzump_competitors.pricing),
                        updated_by=new.updated_by
                    """,
                    (client_code, product_id, name, clamp(url),
                     comp.get("logo_url") or None, clamp(comp.get("location")),
                     clamp(comp.get("pricing")), user_id, user_id),
                )
        await conn.commit()


# ── Competitors + creatives + assets (writer) ────────────────────────────────


def _aspect_ratio_bucket(ratio: float) -> str:
    """Snap a raw width/height ratio to a standard placement ratio, or 'other'.
    A competitor ad arrives at any dimensions; width/height keep the real value."""
    for name, value in _STANDARD_RATIOS:
        if abs(ratio - value) <= _RATIO_TOLERANCE:
            return name
    return "other"


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
        "metrics": creative.metrics,
        "renditions": [r.model_dump(by_alias=True) for r in creative.renditions],
    }


async def sync_competitor(
    client_code: str, product_url: str, competitor: Competitor, user_id: int = 0
) -> int | None:
    """Upsert a competitor and refresh its creatives WHOLESALE for this product.

    One transaction: upsert the adzump_competitors row, delete this competitor's
    creative slice (assets cascade), then re-insert the current creatives + one
    asset each. Mirrors the old 'latest fetch wins, replace wholesale' semantics.
    Returns the competitor row id, or None if the product row is missing.
    """
    pid = await product_id(client_code, product_url)
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
                        url=new.url, logo_url=new.logo_url,
                        location=new.location, pricing=new.pricing,
                        searched_names=new.searched_names,
                        creatives_fetched_at=new.creatives_fetched_at,
                        creative_status=new.creative_status,
                        fetched_creatives=new.fetched_creatives,
                        dropped_creatives=new.dropped_creatives,
                        total_creatives=new.total_creatives,
                        active_creatives=new.active_creatives,
                        updated_by=new.updated_by
                    """,
                    (client_code, pid, competitor.name, competitor.competitor_key or None,
                     competitor.logo_url or None, competitor.location or None,
                     competitor.pricing or None, json.dumps(competitor.searched_names),
                     fetched_at, competitor.fetch_status, fetched, dropped,
                     total, active, user_id, user_id),
                )
                await cur.execute(
                    "SELECT id FROM adzump_competitors "
                    "WHERE client_code=%s AND name=%s AND product_id=%s",
                    (client_code, competitor.name, pid),
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
                        (creative_row_id, _aspect_ratio_bucket(creative.aspect_ratio),
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
                             _aspect_ratio_bucket(rendition.aspect_ratio),
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
