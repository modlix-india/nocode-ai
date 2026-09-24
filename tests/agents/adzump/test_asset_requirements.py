"""AssetRequirements lifecycle + the upload request it drives: fulfil/round-trip
and the asset_upload_request emit. Requirements ride the elicitation payload as
a JSON-safe dict (context is json.dumps'd across turns); the store_* decrements
are tested with asset_manage."""
from __future__ import annotations

import json
import unittest

from app.agents.adzump.agents.product.tools.scrape.assets import (
    _compose_asset_request_text,
)
from app.agents.adzump.agents.product.models import AssetRequirements
from app.agents.adzump.tools.product import _emit_asset_upload_prompt
from tests.agents.adzump._fixtures import FakeStream


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


class EmitPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_emits_and_sets_chip_when_open(self):
        s, ctx = FakeStream(), {}
        out = await _emit_asset_upload_prompt(
            s, AssetRequirements(logo_missing=True, missing_categories=["hero"]),
            ctx, "https://x.com")
        self.assertTrue(out)
        self.assertTrue(s.texts)
        self.assertEqual(s.data[0][0], "asset_upload_request")
        self.assertIn("_pending_suggestions", ctx)  # "Continue without uploading" chip

    async def test_noop_when_nothing_to_ask(self):
        s = FakeStream()
        self.assertFalse(await _emit_asset_upload_prompt(s, AssetRequirements(), {}, "u"))
        self.assertFalse(await _emit_asset_upload_prompt(s, None, {}, "u"))
        self.assertFalse(await _emit_asset_upload_prompt(
            None, AssetRequirements(logo_missing=True), {}, "u"))
        self.assertEqual(s.texts, [])


class ComposeRequestTextTests(unittest.TestCase):
    def test_request_text_only_when_something_is_missing(self):
        for logo, cats, asks in [(True, [], True), (False, ["hero"], True),
                                 (True, ["floor_plan"], True), (False, [], False)]:
            with self.subTest(logo=logo, cats=cats):
                self.assertEqual(bool(_compose_asset_request_text(logo, cats)), asks)


if __name__ == "__main__":
    unittest.main()
