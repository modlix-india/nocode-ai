"""Unit: app/agents/adzump/services/business_storage.py - pure record/helper builders.

Covers `_normalize_url` (the storage key - http→https, www-strip, trailing-slash),
`_build_location_object` (legacy ds-v1 location precedence: map-confirmed →
user-typed → scraped), and `_build_full_record`'s competitive-block honesty
(attempted-wins-over-stale-declined backstop).

Run:
    cd nocode-ai && ./venv/bin/python -m unittest \\
        tests.agents.adzump.tools.test_business_storage -v
"""

from __future__ import annotations

import unittest

from app.agents.adzump.services.business_storage import (
    _normalize_url, _build_location_object, _build_full_record,
)
from tests.agents.adzump._fixtures import RE


class NormalizeUrlLock(unittest.TestCase):

    def test_canonicalises_for_storage_key(self):
        cases = [
            ("http://www.PurvaSparklingSpring.com/villas/", "https://purvasparklingspring.com/villas"),
            ("https://sobha.com", "https://sobha.com"),
            ("http://x.com/", "https://x.com"),
            ("https://www.earthenambience.in/", "https://earthenambience.in"),
            ("", ""),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(_normalize_url(raw), expected)


class BuildLocationObjectLock(unittest.TestCase):

    def test_map_confirmed_wins_with_coords(self):
        loc_meta = {"address": "Sarjapur Road, Bengaluru", "lat": 12.9, "lng": 77.7}
        out = _build_location_object(loc_meta, {"location": "Bengaluru"}, {})
        self.assertEqual(out["product_location"], "Sarjapur Road, Bengaluru")
        self.assertEqual(out["product_coordinates"], {"lng": 77.7, "lat": 12.9})
        self.assertEqual(out["area_location"], "")

    def test_spec_then_scraped_fallback_no_coords(self):
        # No map address → user-typed spec.location wins; no lat/lng → coords None.
        out = _build_location_object(
            {}, {"location": "Whitefield"}, {"place": {"address": "from-site"}})
        self.assertEqual(out["product_location"], "Whitefield")
        self.assertIsNone(out["product_coordinates"])
        # spec empty too → scraped product.place.address.
        out2 = _build_location_object({}, {}, {"place": {"address": "Hosur Road"}})
        self.assertEqual(out2["product_location"], "Hosur Road")


def _rec(spec, *, competitors=None):
    sc = {"product_data": dict(RE), "campaign_spec": dict(spec)}
    if competitors is not None:
        sc["competitor_analysis"] = {"competitors": competitors}
    return _build_full_record(sc, "https://example.com")["campaign"]["competitive"]


class CampaignStatusTests(unittest.TestCase):
    """Stored campaign.status mirrors the launch flag, never asserts it.
    regression: the every-turn autosave hardcoded "launched", so drafts
    persisted as live campaigns from turn 2."""

    def _status(self, spec):
        sc = {"product_data": dict(RE), "campaign_spec": dict(spec)}
        return _build_full_record(sc, "https://example.com")["campaign"]["status"]

    def test_status_variants(self):
        variants = [
            ("pre-launch autosave stores draft", {"platform": "Google Ads"}, "draft"),
            ("launched flag persists as launched",
             {"platform": "Google Ads", "campaign_status": "launched"}, "launched"),
            ("cleared flag reopens the draft",
             {"platform": "Google Ads", "budget": "₹5,000/day"}, "draft"),
        ]
        for label, spec, expected in variants:
            with self.subTest(label):
                self.assertEqual(self._status(spec), expected)


class SessionProvenanceTests(unittest.TestCase):
    """campaign.sessionId carries the chat session id passed by save_campaign.
    regression: PR #91 B7 - the record read `_session_id` straight off
    session context (zero writers), so provenance was always empty."""

    def _session_id(self, chat_session_id):
        sc = {"product_data": dict(RE), "campaign_spec": {"platform": "Google Ads"}}
        record = _build_full_record(sc, "https://example.com", chat_session_id)
        return record["campaign"]["sessionId"]

    def test_stamped_when_given(self):
        self.assertEqual(self._session_id("adzump-C1-42"), "adzump-C1-42")

    def test_empty_when_unknown(self):
        self.assertEqual(self._session_id(""), "")


class LaunchRecordTests(unittest.TestCase):
    """_build_full_record competitive block stays honest. regression: F26 (decline→reverse)."""

    def test_contradiction_persists_as_analyzed_not_declined(self):
        # decline→reverse end state: analysis ran (names) AND stale flag present.
        c = _rec({"platform": "Google Ads", "competitive_analysis_declined": "true"},
                 competitors=[{"name": "Prestige"}, {"name": "Brigade"}])
        self.assertTrue(c["attempted"])
        self.assertFalse(c["declined"], "must not persist declined alongside attempted")

    def test_zero_result_analysis_not_declined(self):
        # reversed + analysis ran but found nothing → attempted, not declined.
        c = _rec({"platform": "Google Ads", "competitive_analysis_declined": "true"},
                 competitors=[])
        self.assertTrue(c["attempted"])
        self.assertFalse(c["declined"])

    def test_genuine_decline_still_persists_declined(self):
        # never analyzed + declined → the real decline must still record.
        c = _rec({"platform": "Google Ads", "competitive_analysis_declined": "true"})
        self.assertFalse(c["attempted"])
        self.assertTrue(c["declined"])

    def test_neither_attempted_nor_declined(self):
        c = _rec({"platform": "Google Ads"})
        self.assertFalse(c["attempted"])
        self.assertFalse(c["declined"])


if __name__ == "__main__":
    unittest.main()
