"""CompetitorProfile - the typed contract for competitor entries.

Locks the lenient parse (legacy shapes, LLM nulls) and the stored-shape dump
(craft's fetched/unfetched tri-state).
"""
from __future__ import annotations

import unittest

from app.agents.adzump.models import CompetitorProfile, ad_previews, competitor_profiles
from app.agents.adzump.models.competitor_profile import CHAT_ADS_PER_COMPETITOR

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
            # The analyst's evidence citation must never collide with the row
            # id (live 2026-09-25: an int field of that name rejected "C6" and
            # every research entry collapsed to its bare name).
            ("analyst citation parses beside the row id",
             {**SCHEMA_ENTRY, "competitor_id": "C6"},
             {"why_competitor": "same buyer pool on the same road", "row_id": None}),
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

    def test_rows(self):
        for label, stored_in, expected in [
            ("unfetched: no key", {"name": "X"}, {}),
            ("fetched, none found", {"name": "X", "creatives": [], "totalCreatives": 0,
                                     "activeCreatives": 0},
             {"creatives": [], "totalCreatives": 0, "activeCreatives": 0}),
            ("fetched ads round-trip by alias",
             {"name": "X", "creatives": [{"creativeId": "1"}], "totalCreatives": 33,
              "activeCreatives": 12},
             {"creatives": [{"creativeId": "1"}], "totalCreatives": 33, "activeCreatives": 12}),
        ]:
            with self.subTest(label):
                stored = CompetitorProfile.from_stored(stored_in).to_stored()
                triad = {k: stored[k] for k in ("creatives", "totalCreatives", "activeCreatives")
                         if k in stored}
                self.assertEqual(triad, expected)
                self.assertEqual(CompetitorProfile.from_stored(stored).to_stored(), stored)


class AdPreviewTests(unittest.TestCase):
    def test_chat_keeps_panel_fields_of_the_latest_ten(self):
        # Full ads (essence, copy, renditions) sank the chat's context past its
        # 64KB column (live 2026-09-25); the chat carries panel fields only.
        full = {"creativeId": "a", "mediaType": "image", "fileUrl": "https://f/a.jpg",
                "headline": "Villas", "isActive": True, "firstSeen": "2026-09-01",
                "essence": {"angle": "lakeside"}, "primaryText": "x" * 500,
                "renditions": [{"fileUrl": "https://f/a-wide.jpg"}]}
        (preview,) = ad_previews([full])
        self.assertNotIn("essence", preview)
        self.assertNotIn("primaryText", preview)
        self.assertEqual((preview["creativeId"], preview["fileUrl"], preview["isActive"]),
                         ("a", "https://f/a.jpg", True))
        ads = [{"creativeId": f"c{i}", "isActive": i >= 8,
                "firstSeen": f"2026-09-{i + 1:02d}"} for i in range(15)]
        ids = [p["creativeId"] for p in ad_previews(ads)]
        self.assertEqual(len(ids), CHAT_ADS_PER_COMPETITOR)
        self.assertEqual(ids[:3], ["c14", "c13", "c12"])  # active first, then newest


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
