"""The public read API for competitor creatives: cache-or-fetch-or-stale.

    for each competitor:
        key = competitor_key(domain)
        record = store.get_competitor(key)
        if record fresh (within freshness window):  serve it
        else:                                        source.fetch(),
                                                     rehost binaries,
                                                     store it, serve it

Because the store is shared, a competitor any client fetched recently is already
warm for everyone - so most calls are cache hits and cost nothing. Misses/stale
entries hit the source (rate-limited, metered), so we run competitors
sequentially and never let one failure abort the batch.

The source is injected (defaults to adlibrary) so a test drives the whole policy
with no network, and a second vendor is a one-line swap.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.agents.adzump import _uploads
from app.agents.adzump.creative_intelligence import store
from app.agents.adzump.creative_intelligence.dedup import dedupe
from app.agents.adzump.creative_intelligence.models import (
    Competitor,
    MAX_CREATIVES_PER_COMPETITOR,
)
from app.agents.adzump.creative_intelligence.sources.adlibrary import (
    AdLibrarySource,
    AdLibraryError,
)
from app.agents.adzump.creative_intelligence.sources.base import AdIntelligenceSource

logger = logging.getLogger(__name__)

# Rehost every creative we keep, so all of them get a content + perceptual hash
# (the dedup + essence-cache keys). Matches the creative cap - a lower binary cap
# would leave the tail unhashed and un-dedupable.
MAX_BINARIES_PER_COMPETITOR = MAX_CREATIVES_PER_COMPETITOR

_DEFAULT_SOURCE = AdLibrarySource()


def competitor_identity(comp: dict) -> tuple[str, str]:
    """Pull (key, name) from a competitor entry as produced by the analysis flow
    (``{name, url, ...}``). ``key`` is the normalized domain; empty when the entry
    has no usable URL/domain."""
    url = comp.get("url") or comp.get("domain") or ""
    return store.competitor_key(url), (comp.get("name") or "").strip()


async def creatives_for(
    *, key: str, name: str, ctx: dict, force: bool = False,
    source: AdIntelligenceSource | None = None,
) -> Competitor | None:
    """Return the stored ``Competitor`` for one competitor, fetching + storing from
    the source on a miss or stale hit. On a source failure, serve whatever stale
    record we already had (better than nothing) rather than raising - the caller
    is batch-oriented."""
    if not key:
        return None
    src = source or _DEFAULT_SOURCE

    record = await store.get_competitor(key, ctx)
    if record and not force and not store.is_stale(record):
        logger.info("creative_intelligence: cache hit key=%s", key)
        return record

    why = "forced" if force else ("stale" if record else "miss")
    logger.info("creative_intelligence: fetching key=%s reason=%s", key, why)
    try:
        fetched = await src.fetch(domain=key, name=name)
    except AdLibraryError as e:
        logger.warning("creative_intelligence: source fetch failed key=%s: %s", key, e)
        return record  # serve stale if we have it; else None

    competitor = Competitor(
        competitor_key=key,
        name=fetched.resolved_name or name,
        domain=key,
        logo_url=fetched.logo_url,
        platform_ids=fetched.platform_ids,
        creatives=fetched.creatives[:MAX_CREATIVES_PER_COMPETITOR],
        last_fetched_at=datetime.now(timezone.utc).isoformat(),
        fetch_status="ok" if fetched.creatives else "empty",
    )
    await _attach_binaries(competitor, ctx)
    # Deterministic dedup cascade: exact (md5) then perceptual (pHash). Vision
    # never culls - it only adds essence (see dedup.py, creative_essence agent).
    competitor.creatives = dedupe(competitor.creatives)
    await store.upsert_competitor(competitor, ctx)
    return competitor


async def creatives_for_all(
    competitors: list[dict], ctx: dict, *, force: bool = False,
    source: AdIntelligenceSource | None = None,
) -> dict[str, Competitor]:
    """Run ``creatives_for`` for each entry. Returns ``{key: Competitor}`` for
    every competitor resolved (cache hit or fresh fetch). Skips entries without a
    usable domain - the source query and our dedup key both need one."""
    results: dict[str, Competitor] = {}
    skipped = 0
    for comp in competitors:
        key, name = competitor_identity(comp)
        if not key:
            skipped += 1
            continue
        if key in results:  # same domain listed twice - fetch once
            continue
        record = await creatives_for(key=key, name=name, ctx=ctx, force=force, source=source)
        if record:
            results[key] = record
    logger.info("creative_intelligence: resolved=%d skipped_no_domain=%d", len(results), skipped)
    return results


async def _attach_binaries(competitor: Competitor, ctx: dict) -> None:
    """Rehost creative images into our file store so the library doesn't depend on
    the source's (undocumented-TTL) URLs. For image ads the creative itself
    (-> fileUrl); for video ads the poster still (-> posterUrl). Video bytes are
    not downloaded (large). Best-effort, bounded, concurrent."""
    key = competitor.competitor_key
    # (creative, source_image_url, sets_poster)
    jobs: list[tuple] = []
    for c in competitor.creatives:
        if c.media_type == "image" and c.source_asset_url:
            jobs.append((c, c.source_asset_url, False))
        elif c.poster_source_url:  # video (or carousel) still
            jobs.append((c, c.poster_source_url, True))
    jobs = jobs[:MAX_BINARIES_PER_COMPETITOR]
    if not jobs:
        return

    async def _one(c, src: str, is_poster: bool) -> None:
        res = await _uploads.rehost_image(
            src, "competitor_creative", ctx, name=f"{key}-{c.creative_id}",
            perceptual=True,
        )
        if res and res.get("url"):
            if is_poster:
                c.poster_url = res["url"]
            else:
                c.file_url = res["url"]
            # md5 = Tier-1 dedup + essence-cache key; pHash = Tier-2 near-dup key.
            c.content_hash = res.get("contentHash", "") or c.content_hash
            c.perceptual_hash = res.get("perceptualHash", "") or c.perceptual_hash

    await asyncio.gather(*(_one(c, s, p) for c, s, p in jobs), return_exceptions=True)
    done = sum(1 for c, _, p in jobs if (c.poster_url if p else c.file_url))
    logger.info("creative_intelligence: rehosted %d/%d images key=%s", done, len(jobs), key)
