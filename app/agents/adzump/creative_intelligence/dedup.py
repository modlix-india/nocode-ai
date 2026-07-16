"""Deterministic creative deduplication - the model-free tiers of the cascade.

    Tier-1  exact       collapse byte-identical re-uploads by content_hash (md5)
    Tier-2  perceptual  collapse re-encoded / resized / renamed copies by pHash

Both keep the higher-signal representative (active > more impressions) and both
are pure Python - no model. The vision pass (Tier-3, elsewhere) extracts essence
and NEVER culls; all deduplication lives here so a distinct creative is only ever
dropped by a deterministic rule.
"""

from __future__ import annotations

from app.agents.adzump.creative_intelligence.models import Creative
from app.agents.adzump.creative_intelligence import phash


def dedupe(creatives: list[Creative]) -> list[Creative]:
    """Full deterministic cascade: exact first (cheap), then perceptual on the
    survivors."""
    return dedupe_perceptual(dedupe_exact(creatives))


def dedupe_exact(creatives: list[Creative]) -> list[Creative]:
    """Tier-1: collapse byte-identical creatives by content hash, keeping the
    higher-signal copy. Creatives with no hash (rehost failed / non-image) are
    all kept - we can't prove they're duplicates."""
    best: dict[str, Creative] = {}
    hashless: list[Creative] = []
    for c in creatives:
        if not c.content_hash:
            hashless.append(c)
            continue
        cur = best.get(c.content_hash)
        if cur is None or _signal(c) > _signal(cur):
            best[c.content_hash] = c
    return list(best.values()) + hashless


def dedupe_perceptual(creatives: list[Creative]) -> list[Creative]:
    """Tier-2: collapse near-identical creatives (same ad re-encoded / resized /
    recolored) by perceptual-hash Hamming distance, keeping the higher-signal
    representative. Brute-force pairwise - fine at <=60 creatives/competitor.
    Creatives with no perceptual hash pass through untouched (never merged on an
    empty hash)."""
    kept: list[Creative] = []
    passthrough: list[Creative] = []
    for c in creatives:
        if not c.perceptual_hash:
            passthrough.append(c)
            continue
        match = next(
            (i for i, k in enumerate(kept)
             if phash.is_near_duplicate(c.perceptual_hash, k.perceptual_hash)),
            None,
        )
        if match is None:
            kept.append(c)
        elif _signal(c) > _signal(kept[match]):
            kept[match] = c  # keep the stronger of the two near-duplicates
    return kept + passthrough


def _signal(c: Creative) -> tuple[int, int]:
    """Rank a creative for 'which duplicate to keep': active beats paused, then
    more impressions."""
    return (int(c.is_active), int(c.metrics.get("impressions") or 0))
