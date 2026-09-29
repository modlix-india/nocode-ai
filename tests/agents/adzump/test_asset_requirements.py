"""The asset_upload_request emit that open AssetRequirements drive. Their
fulfil/round-trip through the saved chat is tested with asset_manage
(StoreDecrementTests)."""
from __future__ import annotations

import unittest

from app.agents.adzump.agents.product.models import AssetRequirements
from app.agents.adzump.tools.product import _emit_asset_upload_prompt
from tests.agents.adzump._fixtures import FakeStream


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


if __name__ == "__main__":
    unittest.main()
