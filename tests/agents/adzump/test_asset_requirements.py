"""AssetRequirements lifecycle + the upload request it drives: fulfil/round-trip,
the asset_upload_request emit, and the request text. Requirements ride the
elicitation payload as a JSON-safe dict (context is json.dumps'd across turns);
the store_* decrements are tested with asset_manage."""
from __future__ import annotations

import json
import unittest

from app.agents.adzump.agents.product.tools.scrape.assets import (
    _compose_asset_request_text,
)
from app.agents.adzump.agents.product.models import AnalysisOutput, AssetRequirements
from app.agents.adzump.tools.product import _emit_asset_upload_prompt


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
