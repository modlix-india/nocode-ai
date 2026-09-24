"""CompetitorProfile - the typed contract for competitor entries.

Locks the lenient parse (legacy shapes, LLM nulls) and the stored-shape dump
(craft's fetched/unfetched tri-state).
"""
from __future__ import annotations

import unittest

from app.agents.adzump.models import CompetitorProfile, competitor_profiles

SCHEMA_ENTRY = {
    "name": "Purva Sparkling Springs",
    "url": None,
    "business_type": "premium apartments",
    "location": "Bengaluru",
    "pricing": None,
    "key_usps": ["lakefront", "clubhouse"],
    "weakness": None,
    "why_competitor": "same buyer pool on the same road",
}

LEGACY_ENTRY = {"product_name": "Lodha Azur", "url": "https://lodhagroup.com"}


class FromStoredTests(unittest.TestCase):
    def test_lenient_parse(self):
        for name, raw, expected in [
            ("schema entry", SCHEMA_ENTRY, {"name": "Purva Sparkling Springs", "url": None}),
            ("legacy product_name folds into name", LEGACY_ENTRY, {"name": "Lodha Azur"}),
            ("LLM nulls fall back to defaults",
             {"name": "X", "key_usps": None, "business_type": None},
             {"key_usps": [], "business_type": ""}),
            ("malformed entry keeps the name", {"name": "X", "key_usps": 7},
             {"name": "X", "key_usps": []}),
        ]:
            with self.subTest(name):
                profile = CompetitorProfile.from_stored(raw)
                for field, value in expected.items():
                    self.assertEqual(getattr(profile, field), value)

    def test_stored_shape(self):
        for name, raw, present, absent in [
            ("schema entry round-trips", SCHEMA_ENTRY, SCHEMA_ENTRY, ()),
            ("legacy product_name is never re-emitted", LEGACY_ENTRY,
             {"name": "Lodha Azur"}, ("product_name",)),
            ("unknown extras survive", {"name": "X", "listing_rank": 3},
             {"listing_rank": 3}, ()),
        ]:
            with self.subTest(name):
                stored = CompetitorProfile.from_stored(raw).to_stored()
                self.assertEqual({k: stored.get(k) for k in present}, present)
                for key in absent:
                    self.assertNotIn(key, stored)


class CreativesTriStateTests(unittest.TestCase):
    """craft.py renders: no key = unfetched (badge-less), [] = "No ads found",
    populated = badge + carousel. The model must preserve all three."""

    def test_unfetched_omits_the_triad(self):
        stored = CompetitorProfile.from_stored({"name": "X"}).to_stored()
        for key in ("creatives", "totalCreatives", "activeCreatives"):
            self.assertNotIn(key, stored)

    def test_fetched_empty_keeps_the_key(self):
        stored = CompetitorProfile.from_stored(
            {"name": "X", "creatives": [], "totalCreatives": 0,
             "activeCreatives": 0}).to_stored()
        self.assertEqual(stored["creatives"], [])
        self.assertEqual(stored["totalCreatives"], 0)

    def test_attach_round_trips_by_alias(self):
        profile = CompetitorProfile.from_stored({"name": "X"})
        profile.creatives = [{"creativeId": "1"}]
        profile.total_creatives = 33
        profile.active_creatives = 12
        stored = profile.to_stored()
        self.assertEqual(stored["totalCreatives"], 33)
        self.assertEqual(stored["activeCreatives"], 12)
        again = CompetitorProfile.from_stored(stored)
        self.assertEqual(again.total_creatives, 33)


class AccessorTests(unittest.TestCase):
    def test_session_entries(self):
        for name, session, expected in [
            ("reads entries, skips junk, folds legacy names",
             {"competitor_analysis": {"competitors": [
                 {"name": "A"}, "junk", {"product_name": "B"}]}}, ["A", "B"]),
            ("empty session", {}, []),
        ]:
            with self.subTest(name):
                self.assertEqual([p.name for p in competitor_profiles(session)], expected)


if __name__ == "__main__":
    unittest.main()
