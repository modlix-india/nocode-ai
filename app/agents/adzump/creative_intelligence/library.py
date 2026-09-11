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

import httpx
from typing import Awaitable, Callable

from app.config import settings
from app.agents.adzump import _uploads
from app.agents.adzump.creative_intelligence import store, taxonomy
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
# Hang deadlines (live 2026-09-08: one hung vision call wedged the batch
# gather - the fetch tool, its turn, and the user's spinner sat open 12+
# minutes). Enrich covers the essence LLM calls (a full per-creative fallback
# round is ~1 min per 12 creatives); process is the backstop over one
# competitor's whole unmetered half.
_ENRICH_TIMEOUT_SECONDS = 400
_PROCESS_TIMEOUT_SECONDS = 600
# Served-URL verification (verify.py) is small GETs against our own file
# server - parallel but polite.
MAX_CONCURRENT_VERIFICATIONS = 6
# The dropped[] diagnostic trail is bounded so a chronically-failing
# competitor can't grow its record without limit.
MAX_DROPPED_ENTRIES = 50

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
    fetched, prior, searched = await _fetch_stage(
        key=key, names=[name], ctx=ctx, force=force, source=source)
    if fetched is None:
        if prior:
            await _stamp_business_url(prior, ctx)
        return prior
    return await _process_stage(key=key, name=name, ctx=ctx,
                                fetched=fetched, prior=prior, enrich=enrich,
                                searched_names=searched)


async def _fetch_stage(
    *, key: str, names: list[str], ctx: dict, force: bool,
    source: AdIntelligenceSource | None,
) -> tuple[SourceFetch | None, Competitor | None, list[str]]:
    """The rate-limited half: cache check + source fetch. Returns
    ``(fetched, prior, searched_names)`` - when ``fetched`` is None, ``prior``
    IS the answer (cache hit, stale-serve on failure, or kept-prior on empty
    fetch); ``searched_names`` is what the resulting record should claim was
    searched (union on an augment, replacement on a full refresh - a refresh
    discards other names' ads, so it must discard their claims too).

    ``names`` - every DISTINCT entry name sharing this key. The ad search is
    name-driven, so a shared domain searches once per name and merges (live
    2026-09-10: 'Purva Symphony' wrongly shared cityville.in and its single
    search returned zero ads for Valmark Cityville, which never got searched
    under its own name). Dedup downstream collapses any overlap.

    A FRESH record only satisfies names it was actually searched under: an
    uncovered name searches just itself and merges with the stored creatives
    (live 2026-09-11: 'Purva Sparkling Springs' resolved to puravankara.com and
    was served the record built under 'Puravankara The Sound of Water' - a
    multi-project parent domain must accrete searches, never let the first
    project own the key for a whole TTL)."""
    src = source or _default_source()

    record = await store.get_competitor(key, ctx)
    to_search = list(names)
    augment = False
    if record and not force and not store.is_stale(record):
        to_search = _uncovered_names(record, names)
        if not to_search:
            logger.info("creative_intelligence: cache hit key=%s", key)
            return None, record, []
        augment = True
        logger.info("creative_intelligence: fresh record, unsearched name(s) "
                    "key=%s missing=%s", key, to_search)

    why = ("augment" if augment
           else "forced" if force else ("stale" if record else "miss"))
    logger.info("creative_intelligence: fetching key=%s reason=%s names=%d",
                key, why, len(to_search))
    # A name-scoped key is not a host - advertiser attribution then runs on
    # name match alone (domain-link matching needs a real domain).
    search_domain = "" if key.startswith("name:") else key
    fetched: SourceFetch | None = None
    searched_ok: list[str] = []
    for name in to_search:
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
        searched_ok.append(name)
        if fetched is None:
            fetched = got
        else:
            fetched.creatives.extend(got.creatives)
            # Identity fields: first name that resolved them wins.
            fetched.resolved_name = fetched.resolved_name or got.resolved_name
            fetched.logo_url = fetched.logo_url or got.logo_url
            fetched.platform_ids = fetched.platform_ids or got.platform_ids
    if fetched is None:  # every name's fetch failed
        return None, record, []  # serve stale if we have it; else None

    if augment:
        # Union: the stored creatives (other names' finds) re-enter the ingest
        # alongside the new name's; dedup collapses overlap, the gate re-judges
        # everything, and the record's claims grow to cover the new name.
        fetched.creatives.extend(record.creatives)
        return fetched, record, _merged_names(record.searched_names, searched_ok)

    if not fetched.creatives and record and record.creatives:
        # A transiently-empty search result must not destroy a good record
        # (shared store: everyone would see zero creatives for a full TTL,
        # and the stored essences would be lost). Serve the prior record.
        logger.warning("creative_intelligence: empty fetch, keeping prior record "
                       "key=%s (%d creatives)", key, len(record.creatives))
        return None, record, []

    return fetched, record, searched_ok


def _uncovered_names(record: Competitor, names: list[str]) -> list[str]:
    """The requested names this record has never been searched under.
    Legacy records predate ``searchedNames`` - their display name stands in
    as the one name they were built under."""
    seen = {n.lower() for n in record.searched_names}
    if not seen and record.name:
        seen = {record.name.lower()}
    return [n for n in names if n and n.lower() not in seen]


def _merged_names(prior: list[str], new: list[str]) -> list[str]:
    seen = {n.lower() for n in prior}
    return list(prior) + [n for n in new if n.lower() not in seen]


async def _process_stage(
    *, key: str, name: str, ctx: dict, fetched: SourceFetch,
    prior: Competitor | None, enrich: EnrichCreatives | None,
    searched_names: list[str] | None = None,
) -> Competitor:
    """The unmetered half: rehost, dedup, VERIFY, essence, store. Safe to
    overlap with other competitors' fetches - nothing here touches the vendor
    API. The record is built fully validated before the ONE store write, so a
    partially-verified record can never be observed (Rule 8)."""
    discovered = len(fetched.creatives)
    competitor = Competitor(
        competitor_key=key,
        name=fetched.resolved_name or name,
        domain="" if key.startswith("name:") else key,
        logo_url=fetched.logo_url,
        platform_ids=fetched.platform_ids,
        creatives=fetched.creatives[:MAX_CREATIVES_PER_COMPETITOR],
        business_urls=_merged_business_urls(prior, ctx),
        # What this record's creatives can answer for (computed by the fetch
        # stage: union on an augment, this fetch's names on a full refresh).
        searched_names=searched_names if searched_names is not None else [name],
        last_fetched_at=datetime.now(timezone.utc).isoformat(),
    )
    binaries = await _attach_binaries(competitor, ctx)
    rehosted = sum(1 for c in competitor.creatives if c.file_url)
    # Deterministic dedup cascade: creative_id, then exact (md5), then
    # perceptual (pHash). Vision never culls - it only adds essence.
    competitor.creatives = dedupe(competitor.creatives)
    # Rule 2/3/6: every asset is verified through the SERVED public URL before
    # it can be written; failures are dropped with a diagnostic entry.
    competitor.creatives, drop_entries, drop_reasons = await _verify_creatives(
        competitor.creatives)
    verified = len(competitor.creatives)

    _carry_forward_essence(prior, competitor)
    await _enrich_essence(competitor, binaries, enrich)

    # Stage C (taxonomy.py): only ads whose category matches the product's may
    # be written - a same-market competitor can absolutely run an ad for
    # something else. Gate OFF (logged) when the product can't be classified
    # or no classifier ran (enrich=None): never reject against a missing
    # yardstick.
    gate_reasons: dict[str, int] = {}
    gate = _product_gate(ctx) if enrich is not None else None
    if gate:
        competitor.creatives, gate_drops, gate_reasons = _gate_creatives(
            competitor.creatives, *gate)
        drop_entries += gate_drops

    competitor.dropped = _dedupe_dropped(
        drop_entries + (prior.dropped if prior else []))[:MAX_DROPPED_ENTRIES]

    # Rule 7: the status must not lie. Everything-failed-verification is a
    # PIPELINE failure; everything-gated-out is a RELEVANCE outcome ("empty" +
    # emptyReason, dropped[] kept). Neither is ever disguised as "this
    # competitor has no ads at all".
    if competitor.creatives:
        competitor.fetch_status = "ok"
        competitor.fetch_error = ""
        competitor.empty_reason = ""
    elif discovered == 0:
        competitor.fetch_status = "empty"
    elif verified and gate_reasons:
        competitor.fetch_status = "empty"
        competitor.empty_reason = max(gate_reasons, key=gate_reasons.get)
    else:
        competitor.fetch_status = "error"
        competitor.fetch_error = (
            f"all {discovered} discovered creatives failed validation: "
            + ", ".join(f"{r}={n}" for r, n in sorted(drop_reasons.items())))

    logger.info(
        "creative_intelligence: ingest key=%s discovered=%d rehosted=%d "
        "verified=%d written=%d dropped=%s gated=%s",
        key, discovered, rehosted, verified, len(competitor.creatives),
        (dict(sorted(drop_reasons.items())) or "{}"),
        (dict(sorted(gate_reasons.items())) or "{}"),
    )

    await store.upsert_competitor(competitor, ctx)
    return competitor


async def _verify_creatives(
    creatives: list[Creative],
) -> tuple[list[Creative], list[dict], dict[str, int]]:
    """Served-URL verification for every creative (bounded concurrency).
    Returns (kept, dropped_diagnostics, reason_counts)."""
    from app.agents.adzump.creative_intelligence import verify

    sem = asyncio.Semaphore(MAX_CONCURRENT_VERIFICATIONS)
    kept: list[Creative] = []
    drop_entries: list[dict] = []
    reasons: dict[str, int] = {}

    async def _check(c: Creative) -> None:
        async with sem:
            ok, reason = await verify.verify_creative(c)
        if ok:
            kept.append(c)
            return
        reasons[reason] = reasons.get(reason, 0) + 1
        drop_entries.append({
            "creativeId": c.creative_id,
            "fileUrl": c.file_url or c.source_asset_url,
            "reason": reason,
            "droppedAt": datetime.now(timezone.utc).isoformat(),
        })

    await asyncio.gather(*(_check(c) for c in creatives))
    # gather scrambles completion order - keep the source ranking.
    order = {id(c): i for i, c in enumerate(creatives)}
    kept.sort(key=lambda c: order[id(c)])
    return kept, drop_entries, reasons


def _product_gate(ctx: dict) -> tuple[str, str] | None:
    """The Stage-C yardstick ``(category, market)`` from the session's product,
    or None when there is no product or Stage A can't place it in the taxonomy
    (an unknown yardstick would reject the whole library - gate off, loudly)."""
    product = (ctx.get("session_context") or {}).get("product_data") or {}
    if not product:
        return None
    category = taxonomy.ensure_product_classified(product)
    if category in ("", "unknown"):
        logger.warning(
            "creative_intelligence: relevance gate OFF - product category "
            "unknown (businessType=%r)", (product.get("business_type") or "")[:80])
        return None
    return category, product.get("product_market", "")


def _gate_creatives(
    creatives: list[Creative], product_category: str, product_market: str,
) -> tuple[list[Creative], list[dict], dict[str, int]]:
    """Stage C: fail-closed relevance gate over classified creatives.
    Returns (accepted, dropped_diagnostics, reason_counts) - a rejection is
    never silent, and a broker/aggregator ad is KEPT (category-relevant),
    only flagged via its essence.advertiser_role."""
    kept: list[Creative] = []
    drops: list[dict] = []
    reasons: dict[str, int] = {}
    for c in creatives:
        ok, reason = taxonomy.gate_creative(product_category, product_market,
                                            c.essence)
        if ok:
            kept.append(c)
            continue
        reasons[reason] = reasons.get(reason, 0) + 1
        drops.append({
            "creativeId": c.creative_id,
            "fileUrl": c.file_url,
            "reason": reason,
            "category": c.essence.category if c.essence else "unknown",
            "categoryConfidence": (c.essence.category_confidence
                                   if c.essence else 0.0),
            "droppedAt": datetime.now(timezone.utc).isoformat(),
        })
    return kept, drops, reasons


def _dedupe_dropped(entries: list[dict]) -> list[dict]:
    """One dropped[] entry per creativeId (first wins - new entries precede the
    prior record's), so re-running a fetch never duplicates the trail."""
    seen: set[str] = set()
    out: list[dict] = []
    for entry in entries:
        cid = entry.get("creativeId") or ""
        if cid in seen:
            continue
        if cid:
            seen.add(cid)
        out.append(entry)
    return out


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
    on_stage: Callable[[str, str, str], Awaitable[None]] | None = None,
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

    ``on_stage(key, name, message)`` - optional async callback for phase-level
    progress ("searching…", "N ads found - saving…") attributed to ONE
    competitor, so a minutes-long batch never leaves the user staring at a
    silent spinner. Same failure contract as ``on_resolved``.

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

    async def _stage(key: str, name: str, message: str) -> None:
        if on_stage is None:
            return
        try:
            await on_stage(key, name, message)
        except Exception as e:
            logger.warning("creative_intelligence: on_stage failed: %s: %s",
                           type(e).__name__, str(e)[:120])

    async def _process_and_deliver(key: str, name: str, fetched: SourceFetch,
                                   prior: Competitor | None,
                                   searched: list[str]) -> None:
        try:
            # Backstop deadline over the whole unmetered half (rehost + dedup
            # + essence + store): one wedged competitor must never hold the
            # batch's gather - and with it the tool, the turn, and the user's
            # spinner - open forever.
            record = await asyncio.wait_for(
                _process_stage(key=key, name=name, ctx=ctx,
                               fetched=fetched, prior=prior, enrich=enrich,
                               searched_names=searched),
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
        await _stage(key, names[0], "searching the ad library…")
        try:
            fetched, prior, searched = await _fetch_stage(
                key=key, names=names, ctx=ctx, force=force, source=source)
        except Exception as e:
            # e.g. a stored record that no longer validates - skip, don't abort.
            logger.warning("creative_intelligence: competitor failed key=%s: %s: %s",
                           key, type(e).__name__, str(e)[:200])
            continue
        if fetched is None:
            if prior:
                await _stage(key, names[0], "already in the library")
                await _stamp_business_url(prior, ctx)
                await _deliver(key, prior)
            continue
        await _stage(key, names[0],
                     f"{len(fetched.creatives)} ads found - saving…")
        tasks.append(asyncio.create_task(
            _process_and_deliver(key, names[0], fetched, prior, searched)))

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
        # Already-rehosted creatives (prior-record entries re-entering via a
        # name-augment) are skipped: their vendor URLs are expired, and the
        # asset already lives in our store (essence recovers bytes from it
        # via _backfill_stored_bytes).
        if c.media_type != "video" and c.source_asset_url:
            if not c.file_url:  # image / carousel / collection: the asset is an image
                jobs.append((c, c.source_asset_url, False))
            continue
        if c.poster_source_url and not c.poster_url:  # video still
            jobs.append((c, c.poster_source_url, True))
        if c.media_type == "video" and c.source_asset_url and not c.file_url:
            video_jobs.append(c)
    jobs = jobs[:MAX_BINARIES_PER_COMPETITOR]
    video_jobs = video_jobs[:MAX_VIDEOS_PER_COMPETITOR]
    if not jobs and not video_jobs:
        return binaries

    async def _one(c, src: str, is_poster: bool) -> None:
        # Rule 1: the vendor URL is signed with an expiry - rehost NOW, retry
        # twice with backoff, and a creative whose asset never lands is
        # DROPPED downstream (never stored with a source url as fileUrl).
        res = None
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
            res = await _uploads.rehost_image(
                src, "competitor_creative", ctx, name=f"{key}-{c.creative_id}",
                perceptual=True,
            )
            if res and res.get("url"):
                break
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
        url = None
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
            url = await _uploads.rehost_video(
                c.source_asset_url, "competitor_creative", ctx,
                name=f"{key}-{c.creative_id}",
            )
            if url:
                break
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


def _carry_forward_essence(prior: Competitor | None, competitor: Competitor) -> None:
    """The essence cache: a refetch re-lists mostly the same images, and essence
    is content-addressed - copy it from the prior stored record by content_hash
    so the vision pass only ever sees genuinely new creatives. Version-checked:
    an essence classified under an older taxonomy is NOT carried, so a
    TAXONOMY_VERSION bump re-classifies existing records on their next ingest."""
    if prior is None:
        return
    known = {c.content_hash: c.essence
             for c in prior.creatives
             if c.content_hash and c.essence
             and c.essence.taxonomy_version == taxonomy.TAXONOMY_VERSION}
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


async def _backfill_stored_bytes(
    competitor: Competitor, binaries: dict[str, tuple[bytes, str]],
) -> None:
    """Recover image bytes for essence-less creatives that weren't rehosted
    this ingest, from their own served asset (video: the poster still) - the
    same public path verification already trusts. Fills ``binaries`` in place;
    failures only log."""
    from app.agents.adzump.creative_intelligence.verify import _served_url

    todo: list[tuple[str, str]] = []  # (content_hash, path)
    for c in competitor.creatives:
        if c.essence is not None or not c.content_hash or c.content_hash in binaries:
            continue
        path = c.poster_url if c.media_type == "video" else c.file_url
        if path:
            todo.append((c.content_hash, path))
    if not todo:
        return

    sem = asyncio.Semaphore(MAX_CONCURRENT_VERIFICATIONS)

    async def _one(content_hash: str, path: str) -> None:
        async with sem:
            try:
                async with httpx.AsyncClient(timeout=20.0,
                                             follow_redirects=True) as client:
                    resp = await client.get(_served_url(path))
                if resp.status_code == 200 and resp.content:
                    binaries[content_hash] = (
                        resp.content,
                        resp.headers.get("content-type") or "image/jpeg")
            except Exception as e:
                logger.warning("creative_intelligence: stored-bytes backfill "
                               "failed path=%s: %s: %s",
                               path[:80], type(e).__name__, str(e)[:120])

    await asyncio.gather(*(_one(h, p) for h, p in todo))
    logger.info("creative_intelligence: stored-bytes backfill %d/%d key=%s",
                sum(1 for h, _ in todo if h in binaries), len(todo),
                competitor.competitor_key)


async def _enrich_essence(
    competitor: Competitor,
    binaries: dict[str, tuple[bytes, str]],
    enrich: EnrichCreatives | None,
) -> None:
    """Tier-3: typed essence for EVERY deduped survivor that still lacks it -
    the relevance gate needs a category on each one, so there is no recency
    filter (deepseek vision made the full pass cheap). Injected hook - the
    domain never constructs it. Never culls directly; a failure leaves essence
    None, which the gate then rejects fail-closed and the next real ingest
    re-attempts."""
    if enrich is None:
        return
    # Prior-record creatives re-entering an ingest (name-augment, taxonomy
    # bump) have no fresh vendor bytes - their own rehosted asset is the
    # source. Best effort: a creative whose bytes can't be recovered stays
    # essence=None and the gate rejects it fail-closed (dropped[], honest).
    await _backfill_stored_bytes(competitor, binaries)
    pending = [
        CreativeImage(creative=c, data=binaries[c.content_hash][0],
                      content_type=binaries[c.content_hash][1])
        for c in competitor.creatives
        if c.essence is None and c.content_hash in binaries
    ]
    if not pending:
        return
    try:
        # Hard deadline: a hung vision call (live 2026-09-08: a gpt-4o-mini
        # essence run never returned; the whole fetch card ticked past 12
        # minutes) must degrade to essence-less creatives, never wedge the
        # batch - the next real ingest re-attempts.
        essences = await asyncio.wait_for(
            enrich(pending, key=competitor.competitor_key, name=competitor.name),
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
            # The vintage the classification was made under - carry-forward
            # and the gate both key off it.
            c.essence.taxonomy_version = taxonomy.TAXONOMY_VERSION
    logger.info("creative_intelligence: essence added %d/%d key=%s",
                len(essences), len(pending), competitor.competitor_key)
