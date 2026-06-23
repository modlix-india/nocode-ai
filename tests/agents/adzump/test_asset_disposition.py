"""Slice 3 — code disposition for reviewed uploads (design C step 2), below the
model. Construct verdicts (the reviewer's hypothetical output) and assert the
store/reject/escalate call, the content-hash dedup, and the product_data write
shape. No model, no upload I/O.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest \
        tests.agents.adzump.test_asset_disposition -v
"""

from __future__ import annotations

import unittest

from app.agents.adzump._asset_store import (
    classify_verdict, dedup_by_content, store_logo, store_creative,
)
from app.agents.adzump.agents.vision.models import ImageVerdict


def _v(**kw) -> ImageVerdict:
    base = dict(idx=0, role="hero", relevant=True, confidence=0.9, needs_user=False)
    base.update(kw)
    return ImageVerdict(**base)


class ClassifyTests(unittest.TestCase):
    """Model-led, explicit-only escalation — no confidence threshold."""

    def test_needs_user_escalates_even_if_otherwise_storable(self):
        # high confidence + a good role, but the model flagged unsure → ask.
        self.assertEqual(classify_verdict(_v(role="logo", needs_user=True, confidence=0.99)), "escalate")

    def test_not_relevant_rejects(self):
        self.assertEqual(classify_verdict(_v(relevant=False, role="hero")), "reject")

    def test_unused_role_rejects(self):
        self.assertEqual(classify_verdict(_v(role="unused")), "reject")

    def test_usable_roles_store(self):
        for role in ("logo", "hero", "amenity", "floor_plan"):
            self.assertEqual(classify_verdict(_v(role=role)), "store", role)

    def test_unknown_or_empty_role_escalates(self):
        self.assertEqual(classify_verdict(_v(role="unknown")), "escalate")
        self.assertEqual(classify_verdict(_v(role="")), "escalate")

    def test_low_confidence_alone_does_NOT_escalate(self):
        # explicit-only policy: a low number without needs_user still stores.
        self.assertEqual(classify_verdict(_v(role="hero", confidence=0.10)), "store")


class DedupTests(unittest.TestCase):

    def test_identical_bytes_collapse(self):
        imgs = [
            {"data": b"AAAA", "content_type": "image/png"},
            {"data": b"BBBB"},
            {"data": b"AAAA"},                      # dup of #0
        ]
        unique = dedup_by_content(imgs)
        self.assertEqual([i["data"] for i in unique], [b"AAAA", b"BBBB"])

    def test_empty(self):
        self.assertEqual(dedup_by_content([]), [])


class StoreShapeTests(unittest.TestCase):
    """The product_data write shape ported from the tool-loop — lock it."""

    def test_store_logo_writes_lists_and_scalars(self):
        pd, sctx = {}, {}
        store_logo(pd, {"url": "https://s/logo.png", "format": "png"}, "logo-dark", sctx)
        self.assertEqual(pd["logo_urls"], ["https://s/logo.png"])
        self.assertEqual(pd["logo_url"], "https://s/logo.png")
        self.assertEqual(pd["logo_source"], "user_upload")
        self.assertEqual(pd["logo_confidence"], 1.0)
        self.assertTrue(sctx["_asset_logo_cleared"])

    def test_second_logo_appends(self):
        pd, sctx = {}, {}
        store_logo(pd, {"url": "https://s/dev.png"}, "developer", sctx)
        store_logo(pd, {"url": "https://s/proj.png"}, "project", sctx)
        self.assertEqual(pd["logo_urls"], ["https://s/dev.png", "https://s/proj.png"])
        self.assertEqual(pd["logo_url"], "https://s/dev.png")   # scalar = first

    def test_store_creative_appends_and_dedups_url(self):
        pd = {}
        self.assertTrue(store_creative(pd, {"url": "https://s/hero.png"}, "hero", "hero", {}))
        self.assertFalse(store_creative(pd, {"url": "https://s/hero.png"}, "hero", "hero", {}))  # dup url
        self.assertEqual(pd["creative_images"], ["https://s/hero.png"])


if __name__ == "__main__":
    unittest.main()
