"""Perceptual hash for near-duplicate creative detection (dedup Tier-2).

A perceptual (DCT) hash fingerprints an image so re-encoded / resized / lightly
recolored / recompressed copies - the "same creative under a different ad id" the
byte-exact md5 tier misses - land within a small Hamming distance. Pure, no model.

The algorithm is ImageHash's ``phash`` (the same recipe as Meta's PDQ): grayscale,
shrink to 64x64, 2-D DCT, keep the 16x16 lowest frequencies, set a bit for every
coefficient above their median. Written on Pillow + stdlib so the dedup tier
doesn't ship scipy/numpy/PyWavelets (~140 MB) for one transform; the pinned
hashes in test_dedup.py prove it still produces ImageHash's exact bits.
"""

from __future__ import annotations

import logging
import math
import statistics
from io import BytesIO

from PIL import Image

logger = logging.getLogger(__name__)

# 16x16 DCT hash = 256 bits. Bigger than the 64-bit default so the conservative
# threshold below can still separate genuinely different ads from re-encodes.
_HASH_SIZE = 16
_IMAGE_SIZE = _HASH_SIZE * 4  # the image is shrunk to 4x the hash side before the DCT
# Two creatives whose perceptual hashes are within this Hamming distance are the
# same creative. Deliberately conservative: two different ads from one brand
# template must NOT merge - anything this misses still reaches the vision pass,
# which describes but never culls. Tune from per-merge logs on real data.
DUPLICATE_MAX_DISTANCE = 12
# Sentinel distance for "cannot compare" (missing/invalid hash) - never a dup.
_UNCOMPARABLE = 1 << 30

# DCT-II basis rows for the low frequencies only: _COSINES[k][n] weighs pixel n
# into frequency k. The standard 2x scale is dropped - the median split ignores it.
_COSINES = [
    [math.cos(math.pi * k * (2 * n + 1) / (2 * _IMAGE_SIZE)) for n in range(_IMAGE_SIZE)]
    for k in range(_HASH_SIZE)
]


def compute_phash(image_bytes: bytes) -> str:
    """Perceptual hash of an image as a 64-char hex string, or "" when the bytes
    are not a decodable raster image (SVG, truncated, unknown format). A ""
    hash means the caller falls back to content-hash-only dedup for that
    creative - never a crash."""
    if not image_bytes:
        return ""
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            gray = img.convert("L").resize(
                (_IMAGE_SIZE, _IMAGE_SIZE), Image.Resampling.LANCZOS)
            pixels = list(gray.getdata())
    except Exception as e:  # non-raster (SVG), truncated bytes, unknown format
        logger.info("phash_skip: %s: %s", type(e).__name__, str(e)[:120])
        return ""
    coefficients = _low_frequencies(pixels)
    median = statistics.median(coefficients)
    bits = "".join("1" if c > median else "0" for c in coefficients)
    return f"{int(bits, 2):0{_HASH_SIZE * _HASH_SIZE // 4}x}"


def distance(a: str, b: str) -> int:
    """Hamming distance between two hex perceptual hashes. Returns a large
    sentinel (never a duplicate) when either hash is empty, unparseable, or of
    a different size."""
    if not a or not b or len(a) != len(b):
        return _UNCOMPARABLE
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except ValueError:
        return _UNCOMPARABLE


def is_near_duplicate(a: str, b: str) -> bool:
    """True when two creatives' perceptual hashes are close enough to be the same
    creative (re-encoded / resized / recolored)."""
    return distance(a, b) <= DUPLICATE_MAX_DISTANCE


def _low_frequencies(pixels: list[int]) -> list[float]:
    """The 16x16 lowest-frequency 2-D DCT coefficients of a 64x64 grayscale
    image, row-major (vertical frequency, then horizontal). Separable: a
    column pass, then a row pass - only the kept frequencies are computed."""
    size = _IMAGE_SIZE
    columns = [pixels[x::size] for x in range(size)]
    vertical = [[sum(w * p for w, p in zip(basis, column)) for column in columns]
                for basis in _COSINES]
    # Rounded so a truly-zero coefficient IS zero: flat-colour images have many,
    # and float leftovers (+-1e-13) around the median otherwise decide ~half
    # the bits (127 of 256 on flat test graphics, vs ImageHash's exact zeros).
    return [round(sum(w * v for w, v in zip(basis, row)), 6)
            for row in vertical for basis in _COSINES]
