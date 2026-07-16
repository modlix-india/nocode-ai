"""Perceptual hash for near-duplicate creative detection (dedup Tier-2).

A perceptual (DCT) hash fingerprints an image so re-encoded / resized / lightly
recolored / recompressed copies - the "same creative under a different ad id" the
byte-exact md5 tier misses - land within a small Hamming distance. Pure, no model.

Leaf module: ``imagehash`` and ``PIL`` are imported lazily inside the functions so
(a) a missing/broken dep degrades Tier-2 to "no perceptual hash" (Tier-1 still
runs) instead of crashing ingest, and (b) importing this never triggers the
creative_intelligence package __init__ (no import cycle through the library).
"""

from __future__ import annotations

import logging
from io import BytesIO

logger = logging.getLogger(__name__)

# 16x16 DCT hash = 256 bits. Bigger than the 64-bit default so the conservative
# threshold below can still separate genuinely different ads from re-encodes.
_HASH_SIZE = 16
# Two creatives whose perceptual hashes are within this Hamming distance are the
# same creative. Deliberately conservative: two different ads from one brand
# template must NOT merge - anything this misses still reaches the vision pass,
# which describes but never culls. Tune from per-merge logs on real data.
DUPLICATE_MAX_DISTANCE = 12
# Sentinel distance for "cannot compare" (missing/invalid hash) - never a dup.
_UNCOMPARABLE = 1 << 30


def compute_phash(image_bytes: bytes) -> str:
    """Perceptual hash of an image as a hex string, or "" when the bytes are not
    a decodable raster image (SVG, truncated, unknown format) or the dep is
    unavailable. A "" hash means the caller falls back to content-hash-only dedup
    for that creative - never a crash."""
    if not image_bytes:
        return ""
    try:
        import imagehash
        from PIL import Image

        with Image.open(BytesIO(image_bytes)) as img:
            return str(imagehash.phash(img, hash_size=_HASH_SIZE))
    except Exception as e:  # missing dep, non-raster (SVG), truncated bytes
        logger.info("phash_skip: %s: %s", type(e).__name__, str(e)[:120])
        return ""


def distance(a: str, b: str) -> int:
    """Hamming distance between two hex perceptual hashes. Returns a large
    sentinel (never a duplicate) when either hash is empty or unparseable."""
    if not a or not b:
        return _UNCOMPARABLE
    try:
        import imagehash

        return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)
    except Exception:
        return _UNCOMPARABLE


def is_near_duplicate(a: str, b: str) -> bool:
    """True when two creatives' perceptual hashes are close enough to be the same
    creative (re-encoded / resized / recolored)."""
    return distance(a, b) <= DUPLICATE_MAX_DISTANCE
