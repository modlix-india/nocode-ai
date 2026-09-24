"""Unit: creative_intelligence dedup - pHash fingerprint + Tier-1/Tier-2 collapse.

The one lock: exact (md5) and perceptual (pHash) dups collapse to the
higher-signal representative, hashless creatives pass through, and a genuinely
distinct creative is NEVER dropped (dedup is deterministic; vision never culls).
The fingerprint itself is pinned to ImageHash's exact bits (see _IMAGEHASH_PINS).
"""
from __future__ import annotations

import random
import unittest
from io import BytesIO

from PIL import Image, ImageDraw

from app.agents.adzump.creative_intelligence import phash
from app.agents.adzump.creative_intelligence.dedup import (
    dedupe,
    dedupe_by_creative_id,
    dedupe_exact,
)
from app.agents.adzump.creative_intelligence.models import Creative

# 64-bit hex hashes: all-ones and one-bit-off are near-dups; ones vs zeros are far apart.
ONES = "ffffffffffffffff"
ONES_1BIT = "fffffffffffffffe"
ZEROS = "0000000000000000"


def _png(img: Image.Image) -> bytes:
    b = BytesIO(); img.save(b, "PNG"); return b.getvalue()


def _jpeg(img: Image.Image) -> bytes:
    b = BytesIO(); img.save(b, "JPEG", quality=60); return b.getvalue()


def _gradient() -> Image.Image:
    im = Image.new("RGB", (400, 400), "white"); d = ImageDraw.Draw(im)
    for i in range(400):
        d.line([(0, i), (400, i)], fill=(i % 256, (i * 2) % 256, 120))
    d.ellipse([100, 100, 300, 300], fill=(200, 30, 30))
    return im


def _other() -> Image.Image:
    im = Image.new("RGB", (400, 400), "navy"); d = ImageDraw.Draw(im)
    d.rectangle([50, 50, 350, 150], fill="yellow")
    return im


def _noise() -> Image.Image:
    rng = random.Random(7)
    return Image.frombytes("RGB", (131, 97), bytes(rng.randrange(256) for _ in range(131 * 97 * 3)))


def _translucent() -> Image.Image:
    return Image.frombytes("RGBA", (180, 120), bytes(
        v for y in range(120) for x in range(180)
        for v in (x % 256, 40, y * 2 % 256, (x + y) % 256)))


# ImageHash 4.3.2 `phash(hash_size=16)` of each image, recorded before phash.py
# moved to Pillow-only: the rewrite must keep producing these exact bits.
# linear_gradient is the tie case (almost every coefficient exactly zero).
_IMAGEHASH_PINS = [
    ("gradient", _gradient, "e1e1661a1e1e79e569e11e1e169ee5a166789e7669a719681a1ae59667899658"),
    ("other", _other, "857a857a7a857a857a857a85857a857a7a857a85857a857a857a857a7a857a85"),
    ("linear_gradient", lambda: Image.linear_gradient("L"),
     "8000000000000000000000000000000000000000000000000000000000000000"),
    ("noise_non_square", _noise, "968449b62dff3d8719a3048f1b574bb8536981dd9097a33715ce5079c1a395d3"),
    ("translucent_rgba", _translucent, "aaa806e4aaea67ff0951056dce7d1af854643a680f6c66c75b05676e83ea4746"),
]


def _c(cid, content="", ph="", active=False, impr=0):
    return Creative(creative_id=cid, content_hash=content, perceptual_hash=ph,
                    is_active=active, metrics={"impressions": impr})


class DedupTests(unittest.TestCase):
    def test_phash_bits_match_imagehash(self):
        for name, make, expected in _IMAGEHASH_PINS:
            with self.subTest(name):
                self.assertEqual(phash.compute_phash(_png(make())), expected)
        for name, a, b, expected in [
            ("one bit apart", ONES, ONES_1BIT, 1),
            ("different sizes never compare", ONES, ONES + "ff", phash._UNCOMPARABLE),
            ("unparseable never compares", ONES, "z" * 16, phash._UNCOMPARABLE),
        ]:
            with self.subTest(name):
                self.assertEqual(phash.distance(a, b), expected)

    def test_fingerprint_and_tier_cascade(self):
        gradient = _gradient()
        h_gradient = phash.compute_phash(_png(gradient))
        with self.subTest("re-encoded/resized copy is a near-dup; different image is not"):
            reencoded = phash.compute_phash(_jpeg(gradient.resize((320, 320))))
            self.assertTrue(phash.is_near_duplicate(h_gradient, reencoded))
            self.assertFalse(phash.is_near_duplicate(
                h_gradient, phash.compute_phash(_png(_other()))))
        with self.subTest("non-decodable bytes degrade to '' and '' never matches"):
            self.assertEqual(phash.compute_phash(b"not-an-image"), "")
            self.assertFalse(phash.is_near_duplicate("", ONES))
        with self.subTest("cascade: exact + near collapse; distinct + hashless kept"):
            out = dedupe([
                _c("1", "AA", ONES, active=True, impr=10),
                _c("2", "AA", ONES),                 # exact dup of 1 (content_hash)
                _c("3", "BB", ONES_1BIT),            # near-dup of 1 (pHash)
                _c("4", "CC", ZEROS, active=True),   # distinct - NEVER dropped
                _c("5", "", ""),                     # hashless -> kept
            ])
            self.assertEqual(sorted(c.creative_id for c in out), ["1", "4", "5"])
        with self.subTest("exact tier keeps the higher-signal representative"):
            out = dedupe_exact([_c("a", "H", impr=10), _c("b", "H", active=True, impr=1)])
            self.assertEqual([c.creative_id for c in out], ["b"])  # active beats impressions
        with self.subTest("creative_id tier: a re-run updates, never duplicates"):
            first = _c("same-id", "H1", "")
            fresher = _c("same-id", "H2", "", active=True)
            out = dedupe_by_creative_id([first, fresher, _c("", "H3", "")])
            self.assertEqual(len(out), 2)
            kept = next(c for c in out if c.creative_id == "same-id")
            self.assertTrue(kept.is_active)  # last one wins


if __name__ == "__main__":
    unittest.main()
