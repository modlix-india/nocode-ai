"""CompetitorProfile - the typed contract for competitor entries.

Locks the lenient parse (legacy shapes, LLM nulls), the stored-shape dump
(craft's fetched/unfetched tri-state), and the grep-level death of the
retired URL guesser and the phantom ``domain`` read.
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

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

ADZUMP_DIR = Path(__file__).resolve().parents[3] / "app" / "agents" / "adzump"


class FromStoredTests(unittest.TestCase):
    def test_schema_entry_round_trips(self):
        profile = CompetitorProfile.from_stored(SCHEMA_ENTRY)
        self.assertEqual(profile.name, "Purva Sparkling Springs")
        self.assertIsNone(profile.url)
        self.assertEqual(profile.to_stored(), SCHEMA_ENTRY)

    def test_legacy_product_name_folds_into_name(self):
        profile = CompetitorProfile.from_stored(
            {"product_name": "Lodha Azur", "url": "https://lodhagroup.com"})
        self.assertEqual(profile.name, "Lodha Azur")
        self.assertNotIn("product_name", profile.to_stored())

    def test_llm_nulls_fall_back_to_defaults(self):
        profile = CompetitorProfile.from_stored(
            {"name": "X", "key_usps": None, "business_type": None})
        self.assertEqual(profile.key_usps, [])
        self.assertEqual(profile.business_type, "")

    def test_unknown_extras_survive_the_round_trip(self):
        stored = CompetitorProfile.from_stored(
            {"name": "X", "listing_rank": 3}).to_stored()
        self.assertEqual(stored["listing_rank"], 3)

    def test_malformed_entry_keeps_the_name(self):
        profile = CompetitorProfile.from_stored({"name": "X", "key_usps": 7})
        self.assertEqual(profile.name, "X")
        self.assertEqual(profile.key_usps, [])


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
    def test_reads_session_entries(self):
        session = {"competitor_analysis": {"competitors": [
            {"name": "A"}, "junk", {"product_name": "B"}]}}
        names = [p.name for p in competitor_profiles(session)]
        self.assertEqual(names, ["A", "B"])

    def test_empty_session(self):
        self.assertEqual(competitor_profiles({}), [])


class RetiredCodeGrepTests(unittest.TestCase):
    """The URL guesser and the phantom domain read must never come back."""

    def _grep(self, pattern: str) -> str:
        result = subprocess.run(
            ["grep", "-rn", pattern, str(ADZUMP_DIR)],
            capture_output=True, text=True)
        return result.stdout

    def test_url_guesser_is_gone(self):
        self.assertEqual(self._grep("_resolve_brand_url"), "")

    def test_phantom_domain_read_is_gone(self):
        self.assertEqual(self._grep('get("domain")'), "")


if __name__ == "__main__":
    unittest.main()
