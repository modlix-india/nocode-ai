"""Unit tests for the keyword-review-panel mutation logic
(app/agents/adzump/agents/campaign/tools/google/keyword_update.py).

Covers the section-aware match-type coercion (positives EXACT/PHRASE, negatives
PHRASE/BROAD), the add/edit/delete rules and their rejection paths, and the
brand/generic isolation guard (_check_section_signal).
"""

# regression: an edit that omits match_type must coerce a stale out-of-section
# value (e.g. a legacy EXACT negative) instead of persisting it verbatim.
from __future__ import annotations

import asyncio
import unittest

from app.agents.adzump.agents.campaign.models import keyword_research
from app.agents.adzump.agents.campaign.tools.google.keyword_update import (
    _coerce_match_type,
    _check_section_signal,
    update_keywords,
)


def _row(keyword: str, match_type: str = "PHRASE", **extra) -> dict:
    return {"keyword": keyword, "volume": 0, "match_type": match_type, **extra}


def _theme(tid: str, label: str, **sections) -> dict:
    return {"theme": tid, "label": label, "positives": [], "negatives": [], **sections}


def _dump(**overrides) -> dict:
    """A keyword_research dump: one theme's ad group per id, overridable per theme."""
    themes = {
        "brand": _theme("brand", "Brand"),
        "generic": _theme("generic", "Generic"),
    }
    for tid, sections in overrides.items():
        themes[tid].update(sections)
    return {"themes": themes, "meta": {}}


def _rows(dump: dict, theme: str, section: str) -> list[dict]:
    return dump["themes"][theme][section]


def _built(ctx: dict) -> dict:
    """The saved keyword set, read the way production reads it. The fixtures seed the
    pre-envelope key, so this also covers the one-way migration on first write."""
    return keyword_research(ctx["session_context"]) or {}


def _ctx(dump: dict, product_name: str = "") -> dict:
    return {
        "session_context": {
            "keyword_research": dump,
            "product_data": {"product_name": product_name},
        },
        "event_stream": None,  # emit_section_update no-ops on a None stream
        "session_id": "sid",
    }


def _run(params: dict, ctx: dict):
    return asyncio.run(update_keywords(params, ctx))


class CoerceMatchTypeTests(unittest.TestCase):
    # (raw, section, fallback) -> expected coerced value
    CASES = [
        # positives keep EXACT/PHRASE
        ("EXACT", "positives", "PHRASE", "EXACT"),
        ("phrase", "positives", "PHRASE", "PHRASE"),
        # positives may never be BROAD -> fall back
        ("BROAD", "positives", "PHRASE", "PHRASE"),
        # negatives keep PHRASE/BROAD
        ("BROAD", "negatives", "PHRASE", "BROAD"),
        ("phrase", "negatives", "PHRASE", "PHRASE"),
        # negatives may never be EXACT -> fall back
        ("EXACT", "negatives", "PHRASE", "PHRASE"),
        # missing raw -> use fallback IF valid for the section...
        (None, "negatives", "BROAD", "BROAD"),
        (None, "positives", "EXACT", "EXACT"),
        # ...but an out-of-section fallback (the review-fix) is itself coerced
        (None, "negatives", "EXACT", "PHRASE"),  # legacy EXACT negative, no new value
        (None, "positives", "BROAD", "PHRASE"),  # impossible-for-positive fallback
        ("garbage", "negatives", "garbage", "PHRASE"),  # both invalid -> safe default
    ]

    def test_table(self):
        for raw, section, fallback, expected in self.CASES:
            with self.subTest(raw=raw, section=section, fallback=fallback):
                self.assertEqual(_coerce_match_type(raw, section, fallback), expected)


class AddTests(unittest.TestCase):
    def test_add_positive_keeps_exact(self):
        ctx = _ctx(_dump())
        res = _run(
            {
                "action": "add",
                "keyword_type": "generic",
                "section": "positives",
                "keyword": "running shoes",
                "match_type": "EXACT",
            },
            ctx,
        )
        self.assertTrue(res.success)
        rows = _rows(_built(ctx), "generic", "positives")
        self.assertEqual(rows[0]["keyword"], "running shoes")
        self.assertEqual(rows[0]["match_type"], "EXACT")

    def test_add_negative_exact_is_coerced_to_phrase(self):
        ctx = _ctx(_dump())
        res = _run(
            {
                "action": "add",
                "keyword_type": "generic",
                "section": "negatives",
                "keyword": "free shoes",
                "match_type": "EXACT",
            },
            ctx,
        )
        self.assertTrue(res.success)
        rows = _rows(_built(ctx), "generic", "negatives")
        self.assertEqual(rows[0]["match_type"], "PHRASE")  # never EXACT for a negative

    def test_add_duplicate_rejected(self):
        ctx = _ctx(_dump(generic={"positives": [_row("running shoes")]}))
        res = _run(
            {
                "action": "add",
                "keyword_type": "generic",
                "section": "positives",
                "keyword": "running shoes",
            },
            ctx,
        )
        self.assertFalse(res.success)
        self.assertIn("already exists", res.error)

    def test_add_conflicts_with_opposite_section_rejected(self):
        ctx = _ctx(_dump(generic={"negatives": [_row("cheap shoes")]}))
        res = _run(
            {
                "action": "add",
                "keyword_type": "generic",
                "section": "positives",
                "keyword": "cheap shoes",
            },
            ctx,
        )
        self.assertFalse(res.success)
        self.assertIn("can't be in both", res.error)

    def test_add_conflicts_with_other_ad_group_positive_rejected(self):
        ctx = _ctx(_dump(brand={"positives": [_row("nike air")]}))
        res = _run(
            {
                "action": "add",
                "keyword_type": "generic",
                "section": "positives",
                "keyword": "nike air",
            },
            ctx,
        )
        self.assertFalse(res.success)
        self.assertIn("positive in two ad groups", res.error)

    def test_cross_ad_group_scan_covers_every_other_ad_group(self):
        # The collision check used to be a brand<->generic flip. With N ad groups it must
        # scan them ALL — a third ad group's positive still blocks.
        dump = _dump()
        dump["themes"]["generic_location"] = _theme(
            "generic_location",
            "Generic · Location",
            positives=[_row("shoes bengaluru")],
        )
        ctx = _ctx(dump)
        res = _run(
            {
                "action": "add",
                "keyword_type": "generic",
                "section": "positives",
                "keyword": "shoes bengaluru",
            },
            ctx,
        )
        self.assertFalse(res.success)
        self.assertIn("positive in two ad groups", res.error)

    def test_unknown_ad_group_rejected_with_what_was_built(self):
        res = _run(
            {
                "action": "add",
                "keyword_type": "nonexistent",
                "section": "positives",
                "keyword": "running shoes",
            },
            _ctx(_dump()),
        )
        self.assertFalse(res.success)
        self.assertIn("No 'nonexistent' ad group", res.error)
        self.assertIn("brand, generic", res.error)  # names what IS built

    def test_add_too_short_rejected(self):
        ctx = _ctx(_dump())
        res = _run(
            {
                "action": "add",
                "keyword_type": "generic",
                "section": "positives",
                "keyword": "a",
            },
            ctx,
        )
        self.assertFalse(res.success)
        self.assertIn("too short", res.error)


class EditTests(unittest.TestCase):
    def test_edit_negative_omitting_match_type_coerces_stale_exact(self):
        # The stored negative carries a legacy EXACT; the edit renames it and sends no
        # match_type. Before the fix the fallback (EXACT) was persisted verbatim.
        ctx = _ctx(
            _dump(
                generic={
                    "negatives": [
                        _row("free trial", match_type="EXACT", reason="freebie")
                    ]
                }
            )
        )
        res = _run(
            {
                "action": "edit",
                "keyword_type": "generic",
                "section": "negatives",
                "old_keyword": "free trial",
                "keyword": "free demo",
            },
            ctx,
        )
        self.assertTrue(res.success)
        rows = _rows(_built(ctx), "generic", "negatives")
        self.assertEqual(rows[0]["keyword"], "free demo")
        self.assertEqual(
            rows[0]["match_type"], "PHRASE"
        )  # coerced, not the stale EXACT

    def test_edit_rename_to_existing_rejected(self):
        ctx = _ctx(
            _dump(generic={"positives": [_row("running shoes"), _row("trail shoes")]})
        )
        res = _run(
            {
                "action": "edit",
                "keyword_type": "generic",
                "section": "positives",
                "old_keyword": "trail shoes",
                "keyword": "running shoes",
            },
            ctx,
        )
        self.assertFalse(res.success)
        self.assertIn("already exists", res.error)

    def test_edit_missing_target_rejected(self):
        ctx = _ctx(_dump(generic={"positives": [_row("running shoes")]}))
        res = _run(
            {
                "action": "edit",
                "keyword_type": "generic",
                "section": "positives",
                "old_keyword": "ghost",
                "keyword": "new",
            },
            ctx,
        )
        self.assertFalse(res.success)
        self.assertIn("not found", res.error)


class DeleteTests(unittest.TestCase):
    def test_delete_existing(self):
        ctx = _ctx(
            _dump(generic={"positives": [_row("running shoes"), _row("trail shoes")]})
        )
        res = _run(
            {
                "action": "delete",
                "keyword_type": "generic",
                "section": "positives",
                "keyword": "trail shoes",
            },
            ctx,
        )
        self.assertTrue(res.success)
        rows = _rows(_built(ctx), "generic", "positives")
        self.assertEqual([r["keyword"] for r in rows], ["running shoes"])

    def test_delete_missing_rejected(self):
        ctx = _ctx(_dump(generic={"positives": [_row("running shoes")]}))
        res = _run(
            {
                "action": "delete",
                "keyword_type": "generic",
                "section": "positives",
                "keyword": "ghost",
            },
            ctx,
        )
        self.assertFalse(res.success)
        self.assertIn("not found", res.error)


class GuardTests(unittest.TestCase):
    def test_invalid_action(self):
        res = _run(
            {
                "action": "frobnicate",
                "keyword_type": "generic",
                "section": "positives",
                "keyword": "x y",
            },
            _ctx(_dump()),
        )
        self.assertFalse(res.success)
        self.assertIn("Invalid action", res.error)

    def test_no_research_in_session(self):
        ctx = {"session_context": {}, "event_stream": None}
        res = _run(
            {
                "action": "add",
                "keyword_type": "generic",
                "section": "positives",
                "keyword": "x y",
            },
            ctx,
        )
        self.assertFalse(res.success)
        self.assertIn("No keyword research", res.error)

    def test_no_session_context(self):
        res = _run({"action": "add"}, {"event_stream": None})
        self.assertFalse(res.success)
        self.assertIn("No session context", res.error)


class SectionSignalTests(unittest.TestCase):
    def test_generic_containing_full_brand_is_blocked(self):
        ctx = {
            "product_data": {"product_name": "Duolingo"},
            "keyword_research": _dump(),
        }
        err = _check_section_signal("duolingo spanish", "generic", ctx)
        self.assertIsNotNone(err)
        self.assertIn("brand ad group", err)

    def test_generic_without_brand_ok(self):
        ctx = {
            "product_data": {"product_name": "Duolingo"},
            "keyword_research": _dump(),
        }
        self.assertIsNone(_check_section_signal("learn spanish", "generic", ctx))

    def test_brand_without_brand_terms_is_blocked(self):
        ctx = {
            "product_data": {"product_name": "Duolingo"},
            "keyword_research": _dump(),
        }
        err = _check_section_signal("language learning app", "brand", ctx)
        self.assertIsNotNone(err)
        self.assertIn("generic ad group", err)

    def test_brand_with_brand_term_ok(self):
        ctx = {
            "product_data": {"product_name": "Duolingo"},
            "keyword_research": _dump(),
        }
        self.assertIsNone(_check_section_signal("duolingo app", "brand", ctx))


if __name__ == "__main__":
    unittest.main()
