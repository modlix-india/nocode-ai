"""asset_manage below the model: text builders (_build_brief, _saved_summary),
verdict disposition (model-led, explicit-only escalation - no confidence
threshold), product_data writers, elicitation-payload decrements.
Regression: PR1a project-identity grounding - story in plans/asset-upload-qa-findings.md."""
import json
import unittest

from app.agents.adzump.agents.product.models import AssetRequirements
from app.agents.adzump.agents.vision.models import ImageVerdict
from app.agents.adzump.tools.asset_manage import (
    _build_brief, _fulfill_requirement, _saved_summary, classify_verdict,
    store_image, store_logo,
)


def _sctx(name="Purva Sparkling Springs", summary="Premium 3BHK villas."):
    return {"product_data": {"product_name": name, "summary": summary}}


class BuildBriefTests(unittest.TestCase):
    def test_carries_user_note(self):
        out = _build_brief(_sctx(), note="this is our logo")
        self.assertIn('The user said about these image(s): "this is our logo"', out)

    def test_no_note_line_when_absent(self):
        self.assertNotIn("The user said about", _build_brief(_sctx(), note=""))
        self.assertNotIn("The user said about", _build_brief(_sctx()))

    def test_project_anchor_and_name(self):
        out = _build_brief(_sctx(name="Sumadhura Epitome"))
        self.assertIn("THIS product", out)
        self.assertIn("Sumadhura Epitome", out)

    def test_note_trimmed_and_capped(self):
        out = _build_brief(_sctx(), note="  x" * 400)
        line = [l for l in out.splitlines() if l.startswith("The user said")][0]
        self.assertLessEqual(len(line), 340)  # 300-char cap + wrapper


class SavedSummaryTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(_saved_summary([]), [])

    def test_dedups_name_equals_role(self):  # PR4: "Saved your logo." not "logo (logo)"
        out = _saved_summary([{"role": "logo", "name": "logo"}])
        self.assertEqual(out[0], "Saved your logo.")

    def test_keeps_distinct_name(self):
        out = _saved_summary([{"role": "floor_plan", "name": "3bhk-plan"}])
        self.assertEqual(out[0], "Saved 3bhk-plan (floor_plan).")

    def test_hedge_on_hero(self):
        out = _saved_summary([{"role": "hero", "name": "hero"}])
        self.assertTrue(any("isn't from this project" in p for p in out))
        self.assertIn("the hero", out[-1])

    def test_hedge_lists_both_brand_roles(self):
        out = _saved_summary([{"role": "hero", "name": "h"}, {"role": "logo", "name": "l"}])
        self.assertIn("the hero or logo", out[-1])

    def test_no_hedge_on_plain_creative(self):
        out = _saved_summary([{"role": "amenity", "name": "pool"}])
        self.assertFalse(any("isn't from this project" in p for p in out))


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


class StoreWriterTests(unittest.TestCase):

    def test_store_logo_upload_wins_then_appends(self):
        pd, sctx = {}, {}
        store_logo(pd, {"url": "https://s/logo.png", "format": "png"}, "logo-dark", sctx)
        self.assertEqual(pd["assets"]["logos"][0]["confidence"], 1.0)  # uploads are unbeatable
        self.assertTrue(sctx["_asset_logo_cleared"])
        store_logo(pd, {"url": "https://s/proj.png"}, "project", sctx)  # 2nd appends
        self.assertEqual([l["url"] for l in pd["assets"]["logos"]],
                         ["https://s/logo.png", "https://s/proj.png"])

    def test_store_image_appends_and_dedups_url(self):
        pd = {}
        self.assertTrue(store_image(pd, {"url": "https://s/hero.png"}, "hero", "hero", {}))
        self.assertFalse(store_image(pd, {"url": "https://s/hero.png"}, "hero", "hero", {}))
        self.assertEqual([i["url"] for i in pd["assets"]["images"]], ["https://s/hero.png"])


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


if __name__ == "__main__":
    unittest.main()
