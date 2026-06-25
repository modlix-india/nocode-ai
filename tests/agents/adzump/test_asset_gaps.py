"""Typed asset-gap state — replaces the _shift3_signal magic key.

Refactor (plans/asset-gaps-refactor.html): the asset gaps the picker can't fill
no longer ride product_data["_shift3_signal"] (a stringly-typed control key on
the business-data dict, mutated ad-hoc in 3 files). Instead:
  - sub-agent → parent: a TYPED AnalysisOutput.asset_gaps (AssetGaps).
  - across turns: the SAME gaps as a JSON-safe payload on the open elicitation
    (_pending_elicitation.payload); uploads decrement it via fulfill_*.

These tests lock the new logic + the deviation-matrix rows from the plan.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump.test_asset_gaps -v
"""
from __future__ import annotations

import json
import unittest

from app.agents.adzump.agents.product.models import AssetGaps, AnalysisOutput
from app.agents.adzump._asset_store import store_logo, store_creative, _fulfill_gap
from app.agents.adzump.tools.product import _emit_asset_upload_prompt


# ── AssetGaps: the type + its single decrement path ───────────────────────────
class AssetGapsTests(unittest.TestCase):
    def test_fulfill_logo_clears_only_logo(self):
        g = AssetGaps(logo_missing=True, missing_categories=["hero"], verdict="partial")
        g.fulfill_logo()
        self.assertFalse(g.logo_missing)
        self.assertEqual(g.missing_categories, ["hero"])   # untouched

    def test_fulfill_category_drops_that_role_only(self):
        g = AssetGaps(missing_categories=["hero", "amenity"])
        g.fulfill_category("hero")
        self.assertEqual(g.missing_categories, ["amenity"])

    def test_fulfill_unknown_category_is_noop(self):
        # deviation: user uploads an asset that wasn't asked for.
        g = AssetGaps(missing_categories=["hero"])
        g.fulfill_category("floor_plan")
        self.assertEqual(g.missing_categories, ["hero"])

    def test_any_open(self):
        self.assertTrue(AssetGaps(logo_missing=True).any_open())
        self.assertTrue(AssetGaps(missing_categories=["hero"]).any_open())
        self.assertFalse(AssetGaps().any_open())

    def test_to_from_dict_round_trip(self):
        g = AssetGaps(logo_missing=True, missing_categories=["hero"], verdict="x")
        self.assertEqual(AssetGaps.from_dict(g.to_dict()), g)

    def test_from_dict_tolerates_junk(self):
        self.assertIsNone(AssetGaps.from_dict(None))
        self.assertIsNone(AssetGaps.from_dict("nope"))
        self.assertEqual(AssetGaps.from_dict({}), AssetGaps())  # all defaults

    def test_payload_is_json_safe(self):
        # D1: the carrier must survive save_context's json.dumps across turns.
        json.dumps({"payload": AssetGaps(missing_categories=["hero"]).to_dict()})

    def test_analysis_output_default_gap_is_none(self):
        self.assertIsNone(AnalysisOutput(product={}, competitive={}).asset_gaps)


class _FakeStream:
    """Records emit_text / emit_data; both are awaited by the prompt fn."""
    def __init__(self):
        self.texts: list[str] = []
        self.data: list[tuple] = []

    async def emit_text(self, t):
        self.texts.append(t)

    async def emit_data(self, name, payload):
        self.data.append((name, payload))


class EmitPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_emits_and_sets_chip_when_gaps_open(self):
        s, ctx = _FakeStream(), {}
        out = await _emit_asset_upload_prompt(
            s, AssetGaps(logo_missing=True, missing_categories=["hero"]), ctx, "https://x.com")
        self.assertTrue(out)
        self.assertTrue(s.texts)                                  # asked the user
        self.assertEqual(s.data[0][0], "asset_upload_request")
        self.assertTrue(s.data[0][1]["logo_missing"])
        # "Continue without uploading" chip so the user needn't type.
        self.assertIn("_pending_suggestions", ctx)

    async def test_noop_when_no_gaps(self):
        s = _FakeStream()
        self.assertFalse(await _emit_asset_upload_prompt(s, AssetGaps(), {}, "https://x.com"))  # empty gaps
        self.assertFalse(await _emit_asset_upload_prompt(s, None, {}, "https://x.com"))          # no gaps object
        self.assertEqual(s.texts, [])

    async def test_noop_when_stream_none(self):
        self.assertFalse(await _emit_asset_upload_prompt(None, AssetGaps(logo_missing=True), {}, "u"))


# ── store: uploads decrement the elicitation payload, not product_data ────────
def _sctx_with_open_elicit(**gaps):
    g = AssetGaps(logo_missing=gaps.get("logo_missing", False),
                  missing_categories=list(gaps.get("missing_categories", [])),
                  verdict=gaps.get("verdict", ""))
    return {"_pending_elicitation": {"id": "e1", "expects": "multi", "payload": g.to_dict()}}


def _payload(sctx):
    return sctx["_pending_elicitation"]["payload"]


class StoreDecrementTests(unittest.TestCase):
    def test_store_logo_clears_logo_missing(self):
        sctx = _sctx_with_open_elicit(logo_missing=True, missing_categories=["hero"])
        store_logo({}, {"url": "https://x/l.png", "format": "png"}, "mylogo", sctx)
        self.assertFalse(_payload(sctx)["logo_missing"])
        self.assertEqual(_payload(sctx)["missing_categories"], ["hero"])   # still open

    def test_store_creative_drops_its_category(self):
        sctx = _sctx_with_open_elicit(missing_categories=["hero", "amenity"])
        ok = store_creative({}, {"url": "https://x/h.jpg", "format": "jpg"}, "hero", "h1", sctx)
        self.assertTrue(ok)
        self.assertEqual(_payload(sctx)["missing_categories"], ["amenity"])

    def test_multi_turn_decrement_on_same_payload(self):
        # deviation: uploads span several turns; expects="multi" keeps it open.
        sctx = _sctx_with_open_elicit(logo_missing=True, missing_categories=["hero"])
        store_logo({}, {"url": "https://x/l.png", "format": "png"}, "l", sctx)
        store_creative({}, {"url": "https://x/h.jpg", "format": "jpg"}, "hero", "h", sctx)
        p = _payload(sctx)
        self.assertFalse(p["logo_missing"])
        self.assertEqual(p["missing_categories"], [])

    def test_payload_stays_a_dict_after_mutation(self):
        # D1: never re-store a live dataclass — context is json.dumps'd.
        sctx = _sctx_with_open_elicit(logo_missing=True)
        store_logo({}, {"url": "https://x/l.png", "format": "png"}, "l", sctx)
        self.assertIsInstance(_payload(sctx), dict)
        json.dumps(sctx["_pending_elicitation"])             # must not raise

    def test_upload_outside_any_elicitation_is_noop(self):
        # no _pending_elicitation open → store still works, just no decrement.
        sctx = {}
        ok = store_creative({}, {"url": "https://x/h.jpg", "format": "jpg"}, "hero", "h", sctx)
        self.assertTrue(ok)
        self.assertNotIn("_pending_elicitation", sctx)

    def test_fulfill_gap_noop_when_payload_absent(self):
        sctx = {"_pending_elicitation": {"id": "e1"}}        # no payload key
        _fulfill_gap(sctx, lambda g: g.fulfill_logo())       # must not raise
        self.assertNotIn("payload", sctx["_pending_elicitation"])


if __name__ == "__main__":
    unittest.main()
