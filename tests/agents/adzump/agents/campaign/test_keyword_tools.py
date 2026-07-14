"""Unit tests for the keyword agent's fixed-logic helpers
(app/agents/adzump/agents/campaign/google/keyword/tools.py):
duplicate-token collapse and the negative-keyword submit guard
(positive-overlap / dedup / safety / match-type coercion).
"""
# regression: negatives that overlap a positive (exact or by token ratio) must be
# dropped, and a submitted EXACT match type must be coerced away (never EXACT).
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.agents.campaign.google.keyword import tools


class CollapseRepeatsTests(unittest.TestCase):
    # keyword -> collapsed candidate (or None when nothing repeats)
    CASES = [
        ("a glasses a", "a glasses"),
        ("buy buy now", "buy now"),
        ("running shoes", None),
        ("one two three", None),
        ("", None),
        ("shoes shoes shoes", "shoes"),
    ]

    def test_table(self):
        for keyword, expected in self.CASES:
            with self.subTest(keyword=keyword):
                self.assertEqual(tools._collapse_repeats(keyword), expected)


def _ctx(positives, kw_type="generic"):
    return {
        "session_context": {
            "kw_positives": [{"keyword": k} for k in positives],
            "kw_type": kw_type,
        }
    }


def _submit(items, ctx):
    # _attach_negative_volumes hits the Planner; stub it — volume attachment is out of scope.
    with mock.patch.object(tools, "_attach_negative_volumes", new=mock.AsyncMock()):
        return asyncio.run(tools._submit_negative_keywords({"keywords": items}, ctx))


class SubmitNegativeTests(unittest.TestCase):
    def test_valid_negatives_kept_and_match_type_coerced(self):
        ctx = _ctx(positives=["running shoes"])
        res = _submit(
            [{"keyword": "free shoes", "reason": "freebie", "match_type": "EXACT"}],
            ctx,
        )
        self.assertTrue(res.success)
        kept = ctx["session_context"]["kw_negatives"]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["keyword"], "free shoes")
        self.assertIn(kept[0]["match_type"], {"PHRASE", "BROAD"})  # never EXACT

    def test_exact_positive_collision_dropped(self):
        ctx = _ctx(positives=["running shoes"])
        res = _submit([{"keyword": "Running Shoes"}], ctx)  # normalizes to a positive
        self.assertFalse(res.success)
        self.assertIn("None of your submitted", res.error)

    def test_high_token_overlap_with_positive_dropped(self):
        # negative {running, shoes} overlaps the positive {buy, running, shoes} at 2/2 = 1.0
        # >= the 0.8 ceiling -> dropped as too close to a real positive.
        ctx = _ctx(positives=["buy running shoes"])
        res = _submit([{"keyword": "running shoes"}], ctx)
        self.assertFalse(res.success)

    def test_low_token_overlap_kept(self):
        # negative {running, shoes, kids} overlaps {buy, running, shoes} at 2/3 = 0.67 < 0.8
        ctx = _ctx(positives=["buy running shoes"])
        res = _submit([{"keyword": "running shoes kids"}], ctx)
        self.assertTrue(res.success)
        self.assertEqual(len(ctx["session_context"]["kw_negatives"]), 1)

    def test_duplicates_deduped(self):
        ctx = _ctx(positives=[])
        res = _submit(
            [{"keyword": "free trial"}, {"keyword": "Free Trial"}, {"keyword": "free trial"}],
            ctx,
        )
        self.assertTrue(res.success)
        self.assertEqual(len(ctx["session_context"]["kw_negatives"]), 1)

    def test_non_dict_items_skipped(self):
        ctx = _ctx(positives=[])
        res = _submit(["just a string", None, {"keyword": "free demo"}], ctx)
        self.assertTrue(res.success)
        self.assertEqual(len(ctx["session_context"]["kw_negatives"]), 1)

    def test_empty_submission_is_success_noop(self):
        # No items at all is not an error (nothing to reject) — kept stays empty.
        ctx = _ctx(positives=[])
        res = _submit([], ctx)
        self.assertTrue(res.success)
        self.assertEqual(ctx["session_context"]["kw_negatives"], [])


if __name__ == "__main__":
    unittest.main()
