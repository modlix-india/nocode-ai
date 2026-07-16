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
from app.agents.adzump.creative_intelligence.enrich import CreativeImage, EnrichCreatives
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
    enrich: EnrichCreatives | None = None,
) -> Competitor | None:
    """Return the stored ``Competitor`` for one competitor, fetching + storing from
    the source on a miss or stale hit. On a source failure, serve whatever stale
    record we already had (better than nothing) rather than raising - the caller
    is batch-oriented.

    ``enrich`` is the injected Tier-3 essence hook (see ``enrich.py``) - it runs
    only on a real ingest (never on a cache hit, empty fetch, or stale-serve)
    and only for deduped survivors that still lack essence, before the ONE
    store write."""
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
    binaries = await _attach_binaries(competitor, ctx)
    # Deterministic dedup cascade: exact (md5) then perceptual (pHash). Vision
    # never culls - it only adds essence (see dedup.py, creative_essence agent).
    competitor.creatives = dedupe(competitor.creatives)
    _carry_forward_essence(record, competitor)
    await _enrich_essence(competitor, binaries, enrich)
    await store.upsert_competitor(competitor, ctx)
    return competitor


async def creatives_for_all(
    competitors: list[dict], ctx: dict, *, force: bool = False,
    source: AdIntelligenceSource | None = None,
    enrich: EnrichCreatives | None = None,
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
        record = await creatives_for(key=key, name=name, ctx=ctx, force=force,
                                     source=source, enrich=enrich)
        if record:
            results[key] = record
    logger.info("creative_intelligence: resolved=%d skipped_no_domain=%d", len(results), skipped)
    return results


async def _attach_binaries(competitor: Competitor, ctx: dict) -> dict[str, tuple[bytes, str]]:
    """Rehost creative images into our file store so the library doesn't depend on
    the source's (undocumented-TTL) URLs. For image ads the creative itself
    (-> fileUrl); for video ads the poster still (-> posterUrl). Video bytes are
    not downloaded (large). Best-effort, bounded, concurrent.

    Returns ``{content_hash: (bytes, content_type)}`` - the rehosted bytes, kept
    so the Tier-3 essence pass analyzes exactly what was hashed, without a
    re-download."""
    key = competitor.competitor_key
    binaries: dict[str, tuple[bytes, str]] = {}
    # (creative, source_image_url, sets_poster)
    jobs: list[tuple] = []
    for c in competitor.creatives:
        if c.media_type == "image" and c.source_asset_url:
            jobs.append((c, c.source_asset_url, False))
        elif c.poster_source_url:  # video (or carousel) still
            jobs.append((c, c.poster_source_url, True))
    jobs = jobs[:MAX_BINARIES_PER_COMPETITOR]
    if not jobs:
        return binaries

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
            if c.content_hash and res.get("imageBytes"):
                binaries[c.content_hash] = (
                    res["imageBytes"], res.get("contentType") or "image/jpeg")

    await asyncio.gather(*(_one(c, s, p) for c, s, p in jobs), return_exceptions=True)
    done = sum(1 for c, _, p in jobs if (c.poster_url if p else c.file_url))
    logger.info("creative_intelligence: rehosted %d/%d images key=%s", done, len(jobs), key)
    return binaries


def _carry_forward_essence(prior: Competitor | None, competitor: Competitor) -> None:
    """The essence cache: a refetch re-lists mostly the same images, and essence
    is content-addressed - copy it from the prior stored record by content_hash
    so the vision pass only ever sees genuinely new creatives."""
    if prior is None:
        return
    known = {c.content_hash: c.essence
             for c in prior.creatives if c.content_hash and c.essence}
    if not known:
        return
    carried = 0
    for c in competitor.creatives:
        if c.essence is None and c.content_hash in known:
            c.essence = known[c.content_hash]
            carried += 1
    if carried:
        logger.info("creative_intelligence: essence carried forward %d/%d key=%s",
                    carried, len(competitor.creatives), competitor.competitor_key)


async def _enrich_essence(
    competitor: Competitor,
    binaries: dict[str, tuple[bytes, str]],
    enrich: EnrichCreatives | None,
) -> None:
    """Tier-3: typed essence for the deduped survivors that still lack it.
    Injected hook - the domain never constructs it. Never culls; a failure
    leaves essence None and the next real ingest re-attempts."""
    if enrich is None:
        return
    pending = [
        CreativeImage(creative=c, data=binaries[c.content_hash][0],
                      content_type=binaries[c.content_hash][1])
        for c in competitor.creatives
        if c.essence is None and c.content_hash in binaries
    ]
    if not pending:
        return
    try:
        essences = await enrich(pending)
    except Exception as e:
        logger.warning("creative_intelligence: enrich failed key=%s: %s",
                       competitor.competitor_key, str(e)[:200])
        return
    for c in competitor.creatives:
        if c.essence is None and c.content_hash in essences:
            c.essence = essences[c.content_hash]
    logger.info("creative_intelligence: essence added %d/%d key=%s",
                len(essences), len(pending), competitor.competitor_key)
