"""CampaignSpec / OfferState - the lenient typed parse over stored spec dicts.

The storage contract stays a plain dict (HLD/LLD §4.6); these lock the
migration behavior: legacy ``*_declined="true"`` markers read as DECLINED,
writes never re-emit legacy keys, unknown keys survive the round-trip.
"""
from __future__ import annotations

import pathlib
import unittest

import app.agents.adzump
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


class MarkerGrepCleanTests(unittest.TestCase):
    """S1-8 - the deleted markers stay deleted, and legacy ``*_declined``
    strings appear only in the sanctioned back-compat seams. A new reader or a
    resurrected marker fails here before it ships."""

    ADZUMP = pathlib.Path(app.agents.adzump.__file__).parent

    def test_offered_markers_are_gone(self):
        for py in self.ADZUMP.rglob("*.py"):
            text = py.read_text()
            for marker in ("_ig_offered", "_competitor_creatives_offered"):
                self.assertNotIn(
                    marker, text,
                    f"{py.relative_to(self.ADZUMP)} still mentions {marker}")

    def test_legacy_declined_mentions_are_allowlisted(self):
        # The seams that MAY mention the legacy keys: the lenient parse (models),
        # write canonicalization + cascade (campaign_data), legacy-rail capture
        # (agent), legacy field-name count map consumer (suggestions).
        allowed = {
            "models/campaign_spec.py",
            "tools/campaign_data.py",
            "agent.py",
            "tools/suggestions.py",
        }
        legacy = ("competitive_analysis_declined", "competitor_creatives_declined",
                  "ig_page_declined")
        offenders = sorted(
            py.relative_to(self.ADZUMP).as_posix()
            for py in self.ADZUMP.rglob("*.py")
            if any(k in py.read_text() for k in legacy)
            and py.relative_to(self.ADZUMP).as_posix() not in allowed
        )
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
