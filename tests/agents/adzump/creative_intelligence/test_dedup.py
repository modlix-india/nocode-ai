"""Unit: creative_intelligence dedup - pHash fingerprint + Tier-1/Tier-2 collapse.

The one lock: exact (md5) and perceptual (pHash) dups collapse to the
higher-signal representative, hashless creatives pass through, and a genuinely
distinct creative is NEVER dropped (dedup is deterministic; vision never culls).
"""
from __future__ import annotations

import unittest
from io import BytesIO

from PIL import Image, ImageDraw

from app.agents.adzump.creative_intelligence import phash
from app.agents.adzump.creative_intelligence.dedup import dedupe, dedupe_exact
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


def _c(cid, content="", ph="", active=False, impr=0):
    return Creative(creative_id=cid, content_hash=content, perceptual_hash=ph,
                    is_active=active, metrics={"impressions": impr})


class DedupTests(unittest.TestCase):
    def test_fingerprint_and_tier_cascade(self):
        gradient = _gradient()
        h_gradient = phash.compute_phash(_png(gradient))
        with self.subTest("stable hex fingerprint"):
            self.assertTrue(h_gradient)
            self.assertTrue(all(c in "0123456789abcdef" for c in h_gradient))
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


if __name__ == "__main__":
    unittest.main()
