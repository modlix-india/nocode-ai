"""Unit: creative_intelligence/phash.py - perceptual hash + near-dup distance.

Locks the Tier-2 dedup fingerprint: a re-encoded/resized copy of an image lands
within the threshold; a genuinely different image does not; and non-decodable
bytes degrade to "" (Tier-1-only) instead of crashing.
"""
from __future__ import annotations

import unittest
from io import BytesIO

from PIL import Image, ImageDraw

from app.agents.adzump.creative_intelligence import phash


def _png(img: Image.Image) -> bytes:
    b = BytesIO(); img.save(b, "PNG"); return b.getvalue()


def _jpeg(img: Image.Image, q: int = 60) -> bytes:
    b = BytesIO(); img.save(b, "JPEG", quality=q); return b.getvalue()


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


class ComputePhashTests(unittest.TestCase):
    def test_hash_is_stable_hex(self):
        h = phash.compute_phash(_png(_gradient()))
        self.assertTrue(h)
        self.assertTrue(all(c in "0123456789abcdef" for c in h))

    def test_reencode_and_resize_is_near_dup(self):
        a = _gradient()
        ha = phash.compute_phash(_png(a))
        a_re = phash.compute_phash(_jpeg(a.resize((320, 320))))  # "same creative, renamed"
        self.assertTrue(phash.is_near_duplicate(ha, a_re))

    def test_different_image_is_not_dup(self):
        self.assertFalse(phash.is_near_duplicate(
            phash.compute_phash(_png(_gradient())),
            phash.compute_phash(_png(_other())),
        ))

    def test_non_image_and_empty_degrade_to_blank(self):
        self.assertEqual(phash.compute_phash(b"not-an-image"), "")
        self.assertEqual(phash.compute_phash(b""), "")

    def test_empty_hash_never_a_duplicate(self):
        self.assertFalse(phash.is_near_duplicate("", "ffffffffffffffff"))
        self.assertGreater(phash.distance("", "ffffffffffffffff"), phash.DUPLICATE_MAX_DISTANCE)


if __name__ == "__main__":
    unittest.main()
