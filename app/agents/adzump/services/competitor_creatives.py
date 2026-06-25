"""Orchestrate competitor-creative fetching against the shared library.

The lookup-or-fetch-and-store policy the whole feature hangs on:

    for each competitor:
        key = competitor_key(domain)
        record = get_competitor(key)            # shared SYSTEM library
        if record fresh (within freshness window):  serve it
        else:                                       fetch from adlibrary.com,
                                                    store back into the library,
                                                    serve the fresh record

Because the library is shared, a competitor any client fetched recently is
already warm for everyone else — so most calls are cache hits and cost nothing.
Misses/stale entries hit adlibrary.com (10 req/min, 1 credit/search), so we run
competitors **sequentially** and never let one failure abort the batch.

Binaries: when ``binaries`` is wired (task #3) we download each creative into
the shared Files store and stamp ``fileUrl`` before persisting; until then the
record carries ``sourceAssetUrl`` (the adlibrary-hosted URL) only.
"""

from __future__ import annotations

import asyncio
import logging

from app.agents.adzump import _uploads
from app.agents.adzump.services import creative_library as lib
from app.agents.adzump.services import adlibrary_client as adlib

logger = logging.getLogger(__name__)

# Cap image rehosts per competitor so a prolific advertiser doesn't stall a
# fetch on dozens of downloads. Videos aren't rehosted for now (kept as their
# adlibrary sourceAssetUrl).
MAX_BINARIES_PER_COMPETITOR = 30


def competitor_identity(comp: dict) -> tuple[str, str]:
    """Pull (key, name) from a competitor entry as produced by the analysis
    flow (``competitor.py`` competitors: ``{name, url, ...}``). ``key`` is the
    normalized domain; empty when the entry has no usable URL/domain."""
    url = comp.get("url") or comp.get("domain") or ""
    return lib.competitor_key(url), (comp.get("name") or "").strip()


async def fetch_for_competitor(
    *, key: str, name: str, ctx: dict, force: bool = False,
) -> dict | None:
    """Return the shared-library record for one competitor, fetching+storing
    from adlibrary.com on a miss or stale hit. ``key`` is a normalized domain.

    On an adlibrary failure we return whatever stale record we already had
    (better than nothing) rather than raising — the caller is batch-oriented."""
    if not key:
        return None

    record = await lib.get_competitor(key, ctx)
    if record and not force and not lib.is_stale(record):
        logger.info("competitor_creatives: cache hit key=%s", key)
        return record

    why = "forced" if force else ("stale" if record else "miss")
    logger.info("competitor_creatives: fetching key=%s reason=%s", key, why)
    try:
        raw_ads = await adlib.fetch_competitor_ads(domain=key, name=name)
    except adlib.AdLibraryError as e:
        logger.warning("competitor_creatives: adlibrary fetch failed key=%s: %s", key, e)
        return record  # serve stale if we have it; else None

    doc = adlib.build_library_record(domain=key, name=name, raw_ads=raw_ads)
    await _attach_binaries(doc, ctx)
    await lib.upsert_competitor(doc, ctx)
    return doc


async def _attach_binaries(record: dict, ctx: dict) -> None:
    """Rehost creative images into our file store so the library doesn't depend
    on adlibrary.com's (undocumented-TTL) URLs.

    Rehosts an image per creative: for image ads the creative itself (→
    ``fileUrl``); for video ads the poster still (→ ``posterUrl``). Video bytes
    themselves are NOT downloaded yet (large) — the video keeps its
    ``sourceAssetUrl`` for link-out.

    Stored under the caller's own account for now — fine because static file
    reads are public, so the URL is readable by every client. Best-effort: a
    failed rehost just leaves the source URL in place. Bounded + concurrent to
    cap fetch latency."""
    key = record.get("competitorKey", "")
    # (creative, source_image_url, destination_field)
    jobs: list[tuple[dict, str, str]] = []
    for c in record.get("creatives") or []:
        if c.get("mediaType") == "image" and c.get("sourceAssetUrl"):
            jobs.append((c, c["sourceAssetUrl"], "fileUrl"))
        elif c.get("posterSourceUrl"):  # video (or carousel) still
            jobs.append((c, c["posterSourceUrl"], "posterUrl"))
    jobs = jobs[:MAX_BINARIES_PER_COMPETITOR]
    if not jobs:
        return

    async def _one(c: dict, src: str, field: str) -> None:
        res = await _uploads.rehost_image(
            src, "competitor_creative", ctx,
            name=f"{key}-{c.get('creativeId') or ''}",
        )
        if res and res.get("url"):
            c[field] = res["url"]

    await asyncio.gather(*(_one(c, s, f) for c, s, f in jobs), return_exceptions=True)
    done = sum(1 for c, _, f in jobs if c.get(f))
    logger.info("competitor_creatives: rehosted %d/%d creative images key=%s",
                done, len(jobs), key)


async def fetch_for_competitors(
    competitors: list[dict], ctx: dict, *, force: bool = False,
) -> dict[str, dict]:
    """Run ``fetch_for_competitor`` for each entry. Returns ``{key: record}``
    for every competitor we resolved (cache hit or fresh fetch). Skips entries
    without a usable domain — adlibrary's brand filter and our dedup key both
    need one."""
    results: dict[str, dict] = {}
    skipped: list[str] = []
    for comp in competitors:
        key, name = competitor_identity(comp)
        if not key:
            skipped.append(name or "?")
            continue
        if key in results:  # same domain listed twice — fetch once
            continue
        record = await fetch_for_competitor(key=key, name=name, ctx=ctx, force=force)
        if record:
            results[key] = record
    logger.info(
        "competitor_creatives: resolved=%d skipped_no_domain=%d",
        len(results), len(skipped),
    )
    return results
