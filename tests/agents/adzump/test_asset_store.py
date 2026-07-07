"""Asset store: verdict disposition, dedup, product_data write shapes, and the
AssetRequirements lifecycle (elicitation payload decremented as uploads land).

Merges the old test_asset_disposition.py + test_asset_requirements.py.
Design locks worth keeping: disposition is model-led with explicit-only
escalation (no confidence threshold); requirements ride the elicitation
payload as a JSON-safe dict (context is json.dumps'd across turns).

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump.test_asset_store -v
"""
from __future__ import annotations

import json
import unittest

from app.agents.adzump._asset_store import (
    _fulfill_requirement, classify_verdict, dedup_by_content, store_image, store_logo,
)
from app.agents.adzump.agents.product.tools.scrape.assets import (
    _compose_asset_request_text,
)
from app.agents.adzump.agents.product.models import AnalysisOutput, AssetRequirements
from app.agents.adzump.agents.vision.models import ImageVerdict
from app.agents.adzump.tools.product import _emit_asset_upload_prompt


def _v(**kw) -> ImageVerdict:
    base = dict(idx=0, role="hero", relevant=True, confidence=0.9, needs_user=False)
    base.update(kw)
    return ImageVerdict(**base)


class ClassifyTests(unittest.TestCase):
    """Model-led, explicit-only escalation - no confidence threshold."""

    def test_table(self):
        cases = [
            (_v(role="logo", needs_user=True, confidence=0.99), "escalate"),  # unsure beats confidence
            (_v(relevant=False, role="hero"), "reject"),
            (_v(role="unused"), "reject"),
            (_v(role="logo"), "store"),
            (_v(role="hero"), "store"),
            (_v(role="amenity"), "store"),
            (_v(role="floor_plan"), "store"),
            (_v(role="unknown"), "escalate"),
            (_v(role=""), "escalate"),
            (_v(role="hero", confidence=0.10), "store"),  # low number alone never escalates
        ]
        for verdict, expected in cases:
            with self.subTest(role=verdict.role, needs_user=verdict.needs_user,
                              relevant=verdict.relevant):
                self.assertEqual(classify_verdict(verdict), expected)


class DedupTests(unittest.TestCase):
    def test_identical_bytes_collapse(self):
        imgs = [{"data": b"AAAA"}, {"data": b"BBBB"}, {"data": b"AAAA"}]
        self.assertEqual([i["data"] for i in dedup_by_content(imgs)], [b"AAAA", b"BBBB"])
        self.assertEqual(dedup_by_content([]), [])


class StoreShapeTests(unittest.TestCase):
    """The product_data["assets"] write shape."""

    def test_store_logo_upload_wins_then_appends(self):
        pd, sctx = {}, {}
        store_logo(pd, {"url": "https://s/logo.png", "format": "png"}, "logo-dark", sctx)
        logo = pd["assets"]["logos"][0]
        self.assertEqual(logo["source"], "user_upload")
        self.assertEqual(logo["confidence"], 1.0)  # uploads are unbeatable
        self.assertTrue(sctx["_asset_logo_cleared"])
        store_logo(pd, {"url": "https://s/proj.png"}, "project", sctx)  # 2nd appends
        self.assertEqual([l["url"] for l in pd["assets"]["logos"]],
                         ["https://s/logo.png", "https://s/proj.png"])

    def test_store_image_appends_and_dedups_url(self):
        pd = {}
        self.assertTrue(store_image(pd, {"url": "https://s/hero.png"}, "hero", "hero", {}))
        self.assertFalse(store_image(pd, {"url": "https://s/hero.png"}, "hero", "hero", {}))
        images = pd["assets"]["images"]
        self.assertEqual([i["url"] for i in images], ["https://s/hero.png"])
        self.assertEqual((images[0]["role"], images[0]["source"]), ("hero", "user_upload"))


class AssetRequirementsTests(unittest.TestCase):
    def test_fulfill_and_any_open(self):
        r = AssetRequirements(logo_missing=True, missing_categories=["hero", "amenity"])
        self.assertTrue(r.any_open())
        r.fulfill_logo()
        self.assertFalse(r.logo_missing)
        r.fulfill_category("hero")
        r.fulfill_category("floor_plan")  # not asked for → noop
        self.assertEqual(r.missing_categories, ["amenity"])
        r.fulfill_category("amenity")
        self.assertFalse(r.any_open())

    def test_dict_round_trip_json_safe(self):
        r = AssetRequirements(logo_missing=True, missing_categories=["hero"], verdict="x")
        json.dumps(r.to_dict())  # rides json.dumps'd context across turns
        self.assertEqual(AssetRequirements.from_dict(r.to_dict()), r)
        self.assertIsNone(AssetRequirements.from_dict(None))
        self.assertIsNone(AssetRequirements.from_dict("nope"))
        self.assertEqual(AssetRequirements.from_dict({}), AssetRequirements())

    def test_analysis_output_default_is_none(self):
        self.assertIsNone(AnalysisOutput(product={}, competitive={}).asset_requirements)


class _FakeStream:
    def __init__(self):
        self.texts, self.data = [], []

    async def emit_text(self, t):
        self.texts.append(t)

    async def emit_data(self, name, payload):
        self.data.append((name, payload))


class EmitPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_emits_and_sets_chip_when_open(self):
        s, ctx = _FakeStream(), {}
        out = await _emit_asset_upload_prompt(
            s, AssetRequirements(logo_missing=True, missing_categories=["hero"]),
            ctx, "https://x.com")
        self.assertTrue(out)
        self.assertTrue(s.texts)
        self.assertEqual(s.data[0][0], "asset_upload_request")
        self.assertIn("_pending_suggestions", ctx)  # "Continue without uploading" chip

    async def test_noop_when_nothing_to_ask(self):
        s = _FakeStream()
        self.assertFalse(await _emit_asset_upload_prompt(s, AssetRequirements(), {}, "u"))
        self.assertFalse(await _emit_asset_upload_prompt(s, None, {}, "u"))
        self.assertFalse(await _emit_asset_upload_prompt(
            None, AssetRequirements(logo_missing=True), {}, "u"))
        self.assertEqual(s.texts, [])


def _sctx_with_open_elicit(**req):
    r = AssetRequirements(logo_missing=req.get("logo_missing", False),
                          missing_categories=list(req.get("missing_categories", [])))
    return {"_pending_elicitation": {"id": "e1", "expects": "multi", "payload": r.to_dict()}}


class StoreDecrementTests(unittest.TestCase):
    """Uploads decrement the elicitation payload - the multi-turn F10 flow."""

    def test_uploads_decrement_across_turns(self):
        sctx = _sctx_with_open_elicit(logo_missing=True, missing_categories=["hero"])
        store_logo({}, {"url": "https://x/l.png", "format": "png"}, "l", sctx)
        payload = sctx["_pending_elicitation"]["payload"]
        self.assertFalse(payload["logo_missing"])
        self.assertEqual(payload["missing_categories"], ["hero"])  # still open
        store_image({}, {"url": "https://x/h.jpg", "format": "jpg"}, "hero", "h", sctx)
        payload = sctx["_pending_elicitation"]["payload"]
        self.assertEqual(payload["missing_categories"], [])
        self.assertIsInstance(payload, dict)
        json.dumps(sctx["_pending_elicitation"])  # never a live instance

    def test_noop_without_open_elicitation(self):
        sctx = {}
        self.assertTrue(store_image({}, {"url": "https://x/h.jpg"}, "hero", "h", sctx))
        self.assertNotIn("_pending_elicitation", sctx)
        sctx = {"_pending_elicitation": {"id": "e1"}}  # no payload key
        _fulfill_requirement(sctx, lambda r: r.fulfill_logo())  # must not raise
        self.assertNotIn("payload", sctx["_pending_elicitation"])


class ComposeRequestTextTests(unittest.TestCase):
    """The user-facing upload prompt body - one combined message per decline."""

    def test_table(self):
        cases = [  # (logo_missing, categories, must_contain, must_not_contain)
            (True, [], "brand logo", "I picked some ad images"),
            (False, ["hero", "amenity", "floor_plan"],
             "a hero shot, an amenity / lifestyle photo, and a floor plan", "brand logo"),
            (False, ["hero", "amenity"], "a hero shot and an amenity / lifestyle photo", None),
            (True, ["floor_plan"], "I'm also missing a floor plan", None),
        ]
        for logo, cats, contains, excludes in cases:
            with self.subTest(logo=logo, cats=cats):
                t = _compose_asset_request_text(logo, cats)
                self.assertIn(contains, t)
                if excludes:
                    self.assertNotIn(excludes, t)
        self.assertEqual(_compose_asset_request_text(False, []), "")


if __name__ == "__main__":
    unittest.main()
