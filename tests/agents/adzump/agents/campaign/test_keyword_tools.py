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

from app.agents.adzump.agents.campaign.google.keyword import constants, tools


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
    # fill_volumes hits the Planner; stub it — volume attachment is out of scope.
    with mock.patch.object(tools, "fill_volumes", new=mock.AsyncMock()):
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
            [
                {"keyword": "free trial"},
                {"keyword": "Free Trial"},
                {"keyword": "free trial"},
            ],
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


def _submit_positives(state: dict, keywords: list[dict], rejected=None):
    params = {"keywords": keywords}
    if rejected is not None:
        params["rejected"] = rejected
    return asyncio.run(
        tools._submit_positive_keywords(params, {"session_context": state})
    )


def _state_with(candidates: list[dict], **extra) -> dict:
    return {"kw_type": "generic", "kw_candidates": candidates, **extra}


class WhyThisKeywordTests(unittest.TestCase):
    """A pick records what it cannot re-derive later."""

    def test_pick_carries_source_seed_volume_at_pick_and_admitted_by(self):
        state = _state_with(
            [{"keyword": "blue running shoes", "volume": 5400, "competition": "HIGH"}],
            kw_provenance={
                "blue running shoes": {"source": "bing", "seed": "running shoes"}
            },
        )
        _submit_positives(
            state,
            [
                {
                    "keyword": "blue running shoes",
                    "match_type": "phrase",
                    "intent": "commercial",
                    "rationale": "core term + colour",
                    "admitted_by": "core term in served area",
                }
            ],
        )
        p = state["kw_positives"][0]
        self.assertEqual(p["source"], "bing")  # which surface found it
        self.assertEqual(p["source_seed"], "running shoes")
        self.assertEqual(
            p["volume_at_pick"], 5400
        )  # volume drifts; the decision's value
        self.assertEqual(p["admitted_by"], "core term in served area")
        self.assertEqual(p["rationale"], "core term + colour")

    def test_planner_originated_keyword_records_what_is_known(self):
        # No autosuggest provenance -> planner, empty seed. Record what we know, invent nothing.
        state = _state_with([{"keyword": "running shoes sale", "volume": 700}])
        _submit_positives(
            state, [{"keyword": "running shoes sale", "match_type": "phrase"}]
        )
        p = state["kw_positives"][0]
        self.assertEqual(p["source"], "planner")
        self.assertEqual(p["source_seed"], "")


class WhyNotThisKeywordTests(unittest.TestCase):
    """The negative space is recorded, not just filtered."""

    def test_top_unselected_candidates_are_recorded_by_volume(self):
        state = _state_with(
            [
                {"keyword": "picked", "volume": 9000},
                {"keyword": "cheap running shoes", "volume": 4400},
                {"keyword": "running shoes repair", "volume": 880},
            ]
        )
        _submit_positives(state, [{"keyword": "picked", "match_type": "phrase"}])
        ledger = {r["keyword"]: r for r in state["kw_rejections"]}
        self.assertNotIn("picked", ledger)  # selected -> not a rejection
        self.assertEqual(ledger["cheap running shoes"]["rule"], "not_selected")
        self.assertEqual(ledger["cheap running shoes"]["volume_at_eval"], 4400)

    def test_the_agents_own_reason_is_merged_in_where_it_named_one(self):
        state = _state_with(
            [
                {"keyword": "picked", "volume": 9000},
                {"keyword": "cheap running shoes", "volume": 4400},
            ]
        )
        _submit_positives(
            state,
            [{"keyword": "picked", "match_type": "phrase"}],
            rejected=[
                {
                    "keyword": "cheap running shoes",
                    "reason": "price-shopper — we sell premium",
                }
            ],
        )
        ledger = {r["keyword"]: r for r in state["kw_rejections"]}
        self.assertEqual(
            ledger["cheap running shoes"]["reason"], "price-shopper — we sell premium"
        )

    def test_ledger_is_capped(self):
        state = _state_with(
            [{"keyword": "picked", "volume": 9000}]
            + [{"keyword": f"kw {i}", "volume": 100 - i} for i in range(80)]
        )
        _submit_positives(state, [{"keyword": "picked", "match_type": "phrase"}])
        self.assertEqual(len(state["kw_rejections"]), constants.MAX_REJECTIONS_RECORDED)

    def test_unselected_with_no_stated_reason_still_carries_the_rule(self):
        state = _state_with(
            [
                {"keyword": "picked", "volume": 9000},
                {"keyword": "running shoes repair", "volume": 880},
            ]
        )
        _submit_positives(state, [{"keyword": "picked", "match_type": "phrase"}])
        r = next(
            r for r in state["kw_rejections"] if r["keyword"] == "running shoes repair"
        )
        self.assertEqual(r["rule"], "not_selected")
        self.assertEqual(
            r["reason"], ""
        )  # honest: the rule + the volume, no invented reason


if __name__ == "__main__":
    unittest.main()
