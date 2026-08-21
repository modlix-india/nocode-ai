"""CampaignSpec / OfferState - the lenient typed parse over stored spec dicts.

The storage contract stays a plain dict (HLD/LLD §4.6); these lock the
migration behavior: legacy ``*_declined="true"`` markers read as DECLINED,
writes never re-emit legacy keys, unknown keys survive the round-trip.
"""
from __future__ import annotations

import unittest

from app.agents.adzump.models import CampaignSpec, OfferState, offer_state


class OfferStateTests(unittest.TestCase):
    def test_from_legacy(self):
        cases = [
            ("true", OfferState.DECLINED),
            ("TRUE", OfferState.DECLINED),
            (" true ", OfferState.DECLINED),
            ("false", OfferState.UNSET),
            ("", OfferState.UNSET),
            (None, OfferState.UNSET),  # ACCEPTED never derives from a marker
        ]
        for marker, expected in cases:
            with self.subTest(marker=marker):
                self.assertIs(OfferState.from_legacy(marker), expected)


class OfferStateReadTests(unittest.TestCase):
    """offer_state - the ONE migration-aware read every consumer uses."""

    def test_table(self):
        cases = [
            ("enum declined", {"competitive_analysis": "declined"}, OfferState.DECLINED),
            ("enum accepted", {"competitive_analysis": "accepted"}, OfferState.ACCEPTED),
            ("legacy marker", {"competitive_analysis_declined": "true"}, OfferState.DECLINED),
            ("enum wins over legacy",
             {"competitive_analysis": "accepted", "competitive_analysis_declined": "true"},
             OfferState.ACCEPTED),
            ("absent", {}, OfferState.UNSET),
            ("garbage value", {"competitive_analysis": "yolo"}, OfferState.UNSET),
            ("none spec", None, OfferState.UNSET),
        ]
        for name, spec, expected in cases:
            with self.subTest(name):
                self.assertIs(offer_state(spec, "competitive_analysis"), expected)


class CampaignSpecStoredTests(unittest.TestCase):
    def test_from_stored_lenient(self):
        spec = CampaignSpec.from_stored({
            "platform": "Meta",
            "ig_page_declined": "true",
            "competitive_analysis_declined": "false",
            "mystery_key": 7,
        })
        self.assertIs(spec.instagram, OfferState.DECLINED)
        self.assertIs(spec.competitive_analysis, OfferState.UNSET)
        self.assertEqual(spec.platform, "Meta")
        self.assertEqual(spec.model_extra.get("mystery_key"), 7)

    def test_to_stored_drops_unset_and_never_reemits_legacy(self):
        stored = CampaignSpec.from_stored({
            "platform": "Meta",
            "duration": "30 days",
            "competitor_creatives_declined": "true",
            "mystery_key": 7,
        }).to_stored()
        self.assertEqual(stored.get("competitor_creatives"), "declined")
        self.assertNotIn("competitor_creatives_declined", stored)
        self.assertNotIn("competitive_analysis", stored)  # UNSET offer dropped
        self.assertNotIn("budget", stored)                # empty default dropped
        self.assertEqual(stored.get("mystery_key"), 7)
        # Round-trip stability: re-parsing the stored dict changes nothing.
        self.assertEqual(CampaignSpec.from_stored(stored).to_stored(), stored)


if __name__ == "__main__":
    unittest.main()
