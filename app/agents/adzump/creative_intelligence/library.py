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
import re
from datetime import datetime, timezone
from typing import Awaitable, Callable

from app.config import settings
from app.agents.adzump import _uploads
from app.agents.adzump.creative_intelligence import store
from app.agents.adzump.models import CompetitorProfile
from app.agents.adzump.creative_intelligence.dedup import dedupe
from app.agents.adzump.creative_intelligence.enrich import CreativeImage, EnrichCreatives
from app.agents.adzump.creative_intelligence.models import (
    Competitor,
    Creative,
    MAX_CREATIVES_PER_COMPETITOR,
)
from app.agents.adzump.creative_intelligence.sources.adlibrary import AdLibrarySource
from app.agents.adzump.creative_intelligence.sources.scrapecreators import (
    ScrapeCreatorsSource,
)
from app.agents.adzump.creative_intelligence.sources.base import (
    AdIntelligenceSource,
    SourceFetch,
)
from app.agents.adzump.services.business_storage import (
    normalize_business_url,
    resolve_url,
)

logger = logging.getLogger(__name__)

# Rehost every creative we keep, so all of them get a content + perceptual hash
# (the dedup + essence-cache keys). Matches the creative cap - a lower binary cap
# would leave the tail unhashed and un-dedupable.
MAX_BINARIES_PER_COMPETITOR = MAX_CREATIVES_PER_COMPETITOR
# Video files are orders of magnitude bigger than stills - rehost only the
# first few per competitor (the craft carousel renders 12 creatives total).
MAX_VIDEOS_PER_COMPETITOR = 6
# Vision (essence) is spent only on creatives with recent market presence - an
# ad neither active nor seen within this window is stale inspiration and stays
# essence=None (stored + rendered all the same).
ESSENCE_RECENCY_DAYS = 30
# Hang deadlines (live 2026-09-08: one hung vision call wedged the batch
# gather - the fetch tool, its turn, and the user's spinner sat open 12+
# minutes). Enrich covers the essence LLM calls (a full per-creative fallback
# round is ~1 min per 12 creatives); process is the backstop over one
# competitor's whole unmetered half.
_ENRICH_TIMEOUT_SECONDS = 400
_PROCESS_TIMEOUT_SECONDS = 600

_SOURCES = {"scrapecreators": ScrapeCreatorsSource, "adlibrary": AdLibrarySource}
_default_source_instance: object | None = None


def _default_source():
    """The configured vendor (settings.ADS_INTEL_SOURCE), built once. Unknown
    values fall back to scrapecreators with a warning - never a crash."""
    global _default_source_instance
    if _default_source_instance is None:
        chosen = (settings.ADS_INTEL_SOURCE or "scrapecreators").lower()
        source_cls = _SOURCES.get(chosen)
        if source_cls is None:
            logger.warning("ADS_INTEL_SOURCE=%r unknown, using scrapecreators", chosen)
            source_cls = ScrapeCreatorsSource
        _default_source_instance = source_cls()
    return _default_source_instance


def _campaign_country(ctx: dict) -> str:
    """ISO alpha-2 country of the campaign's confirmed place, or empty."""
    product = (ctx.get("session_context") or {}).get("product_data") or {}
    return ((product.get("place") or {}).get("country_code") or "").strip()


def _business_url(ctx: dict) -> str:
    """Normalized businessUrl of the session's product (the AISuggestedData
    storage key), or empty when the session has no product yet."""
    return normalize_business_url(resolve_url(ctx.get("session_context") or {}))


def competitor_identity(comp: CompetitorProfile) -> tuple[str, str]:
    """Pull (key, name) from a competitor profile. ``key`` is the normalized
    domain when the profile has a URL, else a name-scoped key.

    The ad search is NAME-driven, so a link-less competitor (an honest no-URL
    entry - pre-launch projects often have no site) must still fetch and cache
    under ``name:<slug>`` (live 2026-09-08: link-less Nambiar was silently
    dropped from every fetch while the user asked for its ads 23 times). Once
    a URL settles, the domain key takes over and the entry refetches fresh."""
    name = comp.name.strip()
    key = store.competitor_key(comp.url or "")
    if not key and name:
        key = "name:" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return key, name


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
    and only for deduped recently-active survivors that still lack essence,
    before the ONE store write."""
    if not key:
        return None
    fetched, prior = await _fetch_stage(
        key=key, names=[name], ctx=ctx, force=force, source=source)
    if fetched is None:
        if prior:
            await _stamp_business_url(prior, ctx)
        return prior
    return await _process_stage(key=key, name=name, ctx=ctx,
                                fetched=fetched, prior=prior, enrich=enrich)


async def _fetch_stage(
    *, key: str, names: list[str], ctx: dict, force: bool,
    source: AdIntelligenceSource | None,
) -> tuple[SourceFetch | None, Competitor | None]:
    """The rate-limited half: cache check + source fetch. Returns
    ``(fetched, prior)`` - when ``fetched`` is None, ``prior`` IS the answer
    (cache hit, stale-serve on failure, or kept-prior on empty fetch).

    ``names`` - every DISTINCT entry name sharing this key. The ad search is
    name-driven, so a shared domain searches once per name and merges (live
    2026-09-10: 'Purva Symphony' wrongly shared cityville.in and its single
    search returned zero ads for Valmark Cityville, which never got searched
    under its own name). Dedup downstream collapses any overlap."""
    src = source or _default_source()

    record = await store.get_competitor(key, ctx)
    if record and not force and not store.is_stale(record):
        logger.info("creative_intelligence: cache hit key=%s", key)
        return None, record

    why = "forced" if force else ("stale" if record else "miss")
    logger.info("creative_intelligence: fetching key=%s reason=%s names=%d",
                key, why, len(names))
    # A name-scoped key is not a host - advertiser attribution then runs on
    # name match alone (domain-link matching needs a real domain).
    search_domain = "" if key.startswith("name:") else key
    fetched: SourceFetch | None = None
    for name in names:
        try:
            got = await src.fetch(domain=search_domain, name=name,
                                  country=_campaign_country(ctx))
        except Exception as e:
            # AdLibraryError, transport errors, bad JSON - ANY source failure
            # for this name is logged; other names still search. All-fail
            # serves stale (the batch contract in the module docstring).
            logger.warning("creative_intelligence: source fetch failed key=%s "
                           "name=%r: %s: %s",
                           key, name, type(e).__name__, str(e)[:200])
            continue
        if fetched is None:
            fetched = got
        else:
            fetched.creatives.extend(got.creatives)
            # Identity fields: first name that resolved them wins.
            fetched.resolved_name = fetched.resolved_name or got.resolved_name
            fetched.logo_url = fetched.logo_url or got.logo_url
            fetched.platform_ids = fetched.platform_ids or got.platform_ids
    if fetched is None:  # every name's fetch failed
        return None, record  # serve stale if we have it; else None

    if not fetched.creatives and record and record.creatives:
        # A transiently-empty search result must not destroy a good record
        # (shared store: everyone would see zero creatives for a full TTL,
        # and the stored essences would be lost). Serve the prior record.
        logger.warning("creative_intelligence: empty fetch, keeping prior record "
                       "key=%s (%d creatives)", key, len(record.creatives))
        return None, record

    return fetched, record


async def _process_stage(
    *, key: str, name: str, ctx: dict, fetched: SourceFetch,
    prior: Competitor | None, enrich: EnrichCreatives | None,
) -> Competitor:
    """The unmetered half: rehost, dedup, essence, store. Safe to overlap with
    other competitors' fetches - nothing here touches the vendor API."""
    competitor = Competitor(
        competitor_key=key,
        name=fetched.resolved_name or name,
        domain="" if key.startswith("name:") else key,
        logo_url=fetched.logo_url,
        platform_ids=fetched.platform_ids,
        creatives=fetched.creatives[:MAX_CREATIVES_PER_COMPETITOR],
        business_urls=_merged_business_urls(prior, ctx),
        last_fetched_at=datetime.now(timezone.utc).isoformat(),
        fetch_status="ok" if fetched.creatives else "empty",
    )
    binaries = await _attach_binaries(competitor, ctx)
    # Deterministic dedup cascade: exact (md5) then perceptual (pHash). Vision
    # never culls - it only adds essence (see dedup.py, creative_essence agent).
    competitor.creatives = dedupe(competitor.creatives)
    _carry_forward_essence(prior, competitor)
    await _enrich_essence(competitor, binaries, enrich)
    await store.upsert_competitor(competitor, ctx)
    return competitor


def _merged_business_urls(prior: Competitor | None, ctx: dict) -> list[str]:
    """The prior record's product associations plus the current session's
    product - a shared record accretes every product that researched it."""
    urls = list(prior.business_urls) if prior else []
    url = _business_url(ctx)
    if url and url not in urls:
        urls.append(url)
    return urls


async def _stamp_business_url(record: Competitor, ctx: dict) -> None:
    """Backfill the current product onto a record served straight from the store
    (cache hit / stale-serve / kept-prior). Only real ingests write, so without
    this a product whose competitors are all cache-warm would never appear in
    ``businessUrls`` - and the creatives page groups the library by that field.
    At most one write per product-competitor pair; a failed stamp only logs,
    the serve itself must never break on it."""
    url = _business_url(ctx)
    if not url or url in record.business_urls:
        return
    record.business_urls.append(url)
    try:
        await store.upsert_competitor(record, ctx)
    except Exception as e:
        logger.warning("creative_intelligence: businessUrl stamp failed key=%s: %s: %s",
                       record.competitor_key, type(e).__name__, str(e)[:200])


async def creatives_for_all(
    competitors: list[CompetitorProfile], ctx: dict, *, force: bool = False,
    source: AdIntelligenceSource | None = None,
    enrich: EnrichCreatives | None = None,
    on_resolved: Callable[[str, Competitor], Awaitable[None]] | None = None,
    on_stage: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, Competitor]:
    """Resolve every entry, pipelined: source fetches stay strictly sequential
    (the vendor is rate-limited and metered), but each competitor's unmetered
    processing (rehost/dedup/essence/store) runs as a background task overlapping
    the NEXT competitor's fetch - wall-clock is dominated by the fetch chain, not
    the sum of everything.

    ``on_resolved(key, record)`` - optional async callback awaited as EACH
    competitor resolves (cache hits immediately, fetched ones as their processing
    lands), so a caller can stream partial results to the user. Callback failures
    are logged, never poison the batch.

    ``on_stage(message)`` - optional async callback for phase-level progress
    ("searching X…", "N ads found - saving…") so a minutes-long batch never
    leaves the user staring at a silent spinner. Same failure contract as
    ``on_resolved``.

    Returns ``{key: Competitor}`` for every competitor resolved. Skips entries
    without a usable domain - the source query and our dedup key both need one."""
    results: dict[str, Competitor] = {}
    skipped = 0
    tasks: list[asyncio.Task] = []

    async def _deliver(key: str, record: Competitor) -> None:
        results[key] = record
        if on_resolved is None:
            return
        try:
            await on_resolved(key, record)
        except Exception as e:
            logger.warning("creative_intelligence: on_resolved failed key=%s: %s: %s",
                           key, type(e).__name__, str(e)[:200])

    async def _stage(message: str) -> None:
        if on_stage is None:
            return
        try:
            await on_stage(message)
        except Exception as e:
            logger.warning("creative_intelligence: on_stage failed: %s: %s",
                           type(e).__name__, str(e)[:120])

    async def _process_and_deliver(key: str, name: str, fetched: SourceFetch,
                                   prior: Competitor | None) -> None:
        try:
            # Backstop deadline over the whole unmetered half (rehost + dedup
            # + essence + store): one wedged competitor must never hold the
            # batch's gather - and with it the tool, the turn, and the user's
            # spinner - open forever.
            record = await asyncio.wait_for(
                _process_stage(key=key, name=name, ctx=ctx,
                               fetched=fetched, prior=prior, enrich=enrich),
                timeout=_PROCESS_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error("creative_intelligence: competitor timed out after "
                         "%ds key=%s - dropped from this batch",
                         _PROCESS_TIMEOUT_SECONDS, key)
            return
        except Exception as e:
            # One competitor must never abort the batch (e.g. an upsert
            # refusal) - the rest still resolve.
            logger.warning("creative_intelligence: competitor failed key=%s: %s: %s",
                           key, type(e).__name__, str(e)[:200])
            return
        await _deliver(key, record)

    # Group first: several entries can share one key (two projects on a
    # developer domain - or a bad URL join upstream). The search is
    # name-driven, so every distinct name in the group gets its own search.
    key_names: dict[str, list[str]] = {}
    for comp in competitors:
        key, name = competitor_identity(comp)
        if not key:
            skipped += 1
            continue
        names = key_names.setdefault(key, [])
        if name and name.lower() not in (n.lower() for n in names):
            names.append(name)

    for key, names in key_names.items():
        await _stage(f"Searching the ad library - {', '.join(names)}…")
        try:
            fetched, prior = await _fetch_stage(
                key=key, names=names, ctx=ctx, force=force, source=source)
        except Exception as e:
            # e.g. a stored record that no longer validates - skip, don't abort.
            logger.warning("creative_intelligence: competitor failed key=%s: %s: %s",
                           key, type(e).__name__, str(e)[:200])
            continue
        if fetched is None:
            if prior:
                await _stage(f"{names[0]}: already in the library")
                await _stamp_business_url(prior, ctx)
                await _deliver(key, prior)
            continue
        await _stage(f"{names[0]}: {len(fetched.creatives)} ads found - saving…")
        tasks.append(asyncio.create_task(
            _process_and_deliver(key, names[0], fetched, prior)))

    if tasks:
        await asyncio.gather(*tasks)  # each task handles its own failure
    logger.info("creative_intelligence: resolved=%d skipped_no_domain=%d", len(results), skipped)
    return results


async def _attach_binaries(competitor: Competitor, ctx: dict) -> dict[str, tuple[bytes, str]]:
    """Rehost creative binaries into our file store so the library doesn't depend
    on the source's (undocumented-TTL) URLs. For image/carousel/collection ads
    the asset itself is an image (-> fileUrl); for video ads BOTH the poster
    still (-> posterUrl) and the video file (-> fileUrl, size-capped - the
    craft click-through must keep playing after the vendor URL expires).
    Best-effort, bounded, concurrent.

    Returns ``{content_hash: (bytes, content_type)}`` - the rehosted bytes, kept
    so the Tier-3 essence pass analyzes exactly what was hashed, without a
    re-download."""
    key = competitor.competitor_key
    binaries: dict[str, tuple[bytes, str]] = {}
    # (creative, source_image_url, sets_poster)
    jobs: list[tuple] = []
    video_jobs: list = []
    for c in competitor.creatives:
        if c.media_type != "video" and c.source_asset_url:
            # image / carousel / collection: the asset itself is an image
            jobs.append((c, c.source_asset_url, False))
            continue
        if c.poster_source_url:  # video still
            jobs.append((c, c.poster_source_url, True))
        if c.media_type == "video" and c.source_asset_url:
            video_jobs.append(c)
    jobs = jobs[:MAX_BINARIES_PER_COMPETITOR]
    video_jobs = video_jobs[:MAX_VIDEOS_PER_COMPETITOR]
    if not jobs and not video_jobs:
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

    async def _one_video(c) -> None:
        url = await _uploads.rehost_video(
            c.source_asset_url, "competitor_creative", ctx,
            name=f"{key}-{c.creative_id}",
        )
        if url:
            c.file_url = url  # dedup hashes stay on the poster still

    await asyncio.gather(
        *(_one(c, s, p) for c, s, p in jobs),
        *(_one_video(c) for c in video_jobs),
        return_exceptions=True,
    )
    done = sum(1 for c, _, p in jobs if (c.poster_url if p else c.file_url))
    videos_done = sum(1 for c in video_jobs if c.file_url)
    logger.info("creative_intelligence: rehosted %d/%d images %d/%d videos key=%s",
                done, len(jobs), videos_done, len(video_jobs), key)
    return binaries


def _recently_active(c: Creative) -> bool:
    """Essence eligibility: active now, or last seen within the recency window.
    No parseable ``last_seen`` means no recency evidence - not eligible."""
    if c.is_active:
        return True
    if not c.last_seen:
        return False
    try:
        seen = datetime.fromisoformat(c.last_seen)
    except ValueError:
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - seen).days <= ESSENCE_RECENCY_DAYS


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
        if c.essence is None and c.content_hash in binaries and _recently_active(c)
    ]
    if not pending:
        return
    try:
        # Hard deadline: a hung vision call (live 2026-09-08: a gpt-4o-mini
        # essence run never returned; the whole fetch card ticked past 12
        # minutes) must degrade to essence-less creatives, never wedge the
        # batch - the next real ingest re-attempts.
        essences = await asyncio.wait_for(enrich(pending),
                                          timeout=_ENRICH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning("creative_intelligence: enrich timed out after %ds "
                       "key=%s n=%d - shipping without essence",
                       _ENRICH_TIMEOUT_SECONDS, competitor.competitor_key,
                       len(pending))
        return
    except Exception as e:
        logger.warning("creative_intelligence: enrich failed key=%s: %s",
                       competitor.competitor_key, str(e)[:200])
        return
    for c in competitor.creatives:
        if c.essence is None and c.content_hash in essences:
            c.essence = essences[c.content_hash]
    logger.info("creative_intelligence: essence added %d/%d key=%s",
                len(essences), len(pending), competitor.competitor_key)
