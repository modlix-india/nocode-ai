"""Unit: app/agents/adzump/services/business_storage.py - pure record/helper builders.

Covers `normalize_business_url` (the storage key - http→https, www-strip, trailing-slash),
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
    normalize_business_url, _build_location_object, _build_full_record,
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
                self.assertEqual(normalize_business_url(raw), expected)


class BuildLocationObjectLock(unittest.TestCase):

    def test_confirmed_place_wins_with_coords(self):
        # product.place is the single confirmed location - it wins, carries coords.
        product = {"place": {"address": "Sarjapur Road, Bengaluru", "lat": 12.9, "lng": 77.7}}
        out = _build_location_object({"location": "Bengaluru"}, product)
        self.assertEqual(out["product_location"], "Sarjapur Road, Bengaluru")
        self.assertEqual(out["product_coordinates"], {"lng": 77.7, "lat": 12.9})
        self.assertEqual(out["area_location"], "")

    def test_spec_fallback_when_place_addressless_no_coords(self):
        # place has coords-less/empty address → user-typed spec.location fills in.
        out = _build_location_object({"location": "Whitefield"}, {"place": {}})
        self.assertEqual(out["product_location"], "Whitefield")
        self.assertIsNone(out["product_coordinates"])
        # place.address present → it wins over spec.
        out2 = _build_location_object(
            {"location": "Whitefield"}, {"place": {"address": "Hosur Road"}})
        self.assertEqual(out2["product_location"], "Hosur Road")


def _rec(spec, *, competitors=None):
    sc = {"product_data": dict(RE), "campaign_spec": dict(spec)}
    if competitors is not None:
        sc["competitor_analysis"] = {"competitors": competitors}
    return _build_full_record(sc, "https://example.com")["campaign"]["competitive"]


class ProductCategoryMirrorTests(unittest.TestCase):
    """Stage A fields (taxonomy.py) reach the Modlix mirror record - DS
    launch-time readers see the classification adzump derived."""

    def test_mirror_carries_classification(self):
        product = {**RE, "category": "residential_apartment",
                   "category_override": "residential_villa"}
        record = _build_full_record({"product_data": product},
                                    "https://example.com")
        self.assertEqual(record["category"], "residential_apartment")
        self.assertEqual(record["categoryOverride"], "residential_villa")


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

    def test_enum_decline_persists_identically(self):
        # S1-2 - the enum spec and the legacy spec produce the identical
        # durable record (the ds JSON shape never changes).
        c = _rec({"platform": "Google Ads", "competitive_analysis": "declined"})
        self.assertFalse(c["attempted"])
        self.assertTrue(c["declined"])

    def test_neither_attempted_nor_declined(self):
        c = _rec({"platform": "Google Ads"})
        self.assertFalse(c["attempted"])
        self.assertFalse(c["declined"])


if __name__ == "__main__":
    unittest.main()


class MySQLFirstPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """save_campaign: MySQL is the store of record, Modlix a warn-only mirror
    without the campaign sub-object. hydrate_from_storage: MySQL first, legacy
    MySQL miss is a fresh start (the Modlix mirror is never read back)."""

    SESSION = {
        "product_profile": {"url": "https://springs.com"},
        "product_data": {"product_name": "Springs", "business_type": "real estate",
                         "summary": "villas"},
        "campaign_spec": {"platform": "Meta", "duration": "30 days"},
        "competitor_analysis": {"competitors": [{"name": "Sobha"}]},
        "_session_id": "sess-1",
    }
    CTX = {"client_code": "GRMEL", "user_id": 7, "session_id": "sess-1"}

    def _patches(self):
        from unittest import mock
        return (
            mock.patch("app.agents.adzump.creative_store.upsert_product",
                       new=mock.AsyncMock(return_value=42)),
            mock.patch("app.agents.adzump.creative_store.upsert_flow",
                       new=mock.AsyncMock()),
            mock.patch("app.agents.adzump.creative_store.sync_competitor_profiles",
                       new=mock.AsyncMock()),
            mock.patch("app.agents.adzump.services.business_storage._mirror_modlix_record",
                       new=mock.AsyncMock(return_value="rec-1")),
        )

    async def test_mysql_written_then_mirror_without_campaign(self):
        from app.agents.adzump.services import business_storage as bs
        p_prod, p_camp, p_profiles, p_mirror = self._patches()
        with p_prod as m_prod, p_camp as m_camp, \
             p_profiles as m_profiles, p_mirror as m_mirror:
            result = await bs.save_campaign(dict(self.SESSION), dict(self.CTX))
        self.assertEqual(result, "rec-1")
        m_prod.assert_awaited_once()
        draft = m_camp.await_args.args[5]
        self.assertEqual(draft["platform"], "Meta")
        self.assertEqual(draft["competitors"], [{"name": "Sobha"}])
        # Curated competitors get profile rows at save time, not fetch time.
        self.assertEqual(m_profiles.await_args.args[2], [{"name": "Sobha"}])
        mirror_record = m_mirror.await_args.args[0]
        self.assertNotIn("campaign", mirror_record)

    async def test_mysql_failure_raises_mirror_failure_does_not(self):
        from unittest import mock
        from app.agents.adzump.services import business_storage as bs
        p_prod, p_camp, p_profiles, p_mirror = self._patches()
        with p_prod, p_camp as m_camp, p_profiles, p_mirror:
            m_camp.side_effect = RuntimeError("db down")
            with self.assertRaises(RuntimeError):
                await bs.save_campaign(dict(self.SESSION), dict(self.CTX))
        p_prod2, p_camp2, p_profiles2, p_mirror2 = self._patches()
        with p_prod2, p_camp2, p_profiles2, p_mirror2 as m_mirror:
            m_mirror.return_value = None  # mirror failed internally, warn-only
            result = await bs.save_campaign(dict(self.SESSION), dict(self.CTX))
        self.assertIsNone(result)

    async def test_hydrate_mysql_hit_skips_modlix(self):
        from unittest import mock
        from app.agents.adzump.services import business_storage as bs
        from app.agents.adzump.models.product import Product
        product = Product(product_name="Springs", summary="villas")
        draft = {"location": {"address": "Hebbal, Bangalore"},
                 "competitors": [{"name": "Sobha"}]}
        session_ctx: dict = {}
        with mock.patch("app.agents.adzump.creative_store.get_product",
                        new=mock.AsyncMock(return_value=product)), \
             mock.patch("app.agents.adzump.creative_store.product_id",
                        new=mock.AsyncMock(return_value=42)), \
             mock.patch("app.agents.adzump.creative_store.latest_flow",
                        new=mock.AsyncMock(return_value=draft)), \
             mock.patch.object(bs, "get_by_url",
                               new=mock.AsyncMock()) as m_modlix:
            hit = await bs.hydrate_from_storage("https://springs.com", session_ctx, dict(self.CTX))
        self.assertTrue(hit)
        m_modlix.assert_not_awaited()
        self.assertEqual(session_ctx["product_data"]["product_name"], "Springs")
        self.assertEqual(session_ctx["campaign_spec"]["location"], "Hebbal, Bangalore")
        self.assertEqual(session_ctx["competitor_analysis"]["competitors"], [{"name": "Sobha"}])

    async def test_hydrate_mysql_miss_is_fresh_start(self):
        # MySQL is the ONLY hydration source: a miss returns False without
        # ever reading the Modlix mirror (write-only for DS).
        from unittest import mock
        from app.agents.adzump.services import business_storage as bs
        with mock.patch("app.agents.adzump.creative_store.get_product",
                        new=mock.AsyncMock(return_value=None)), \
             mock.patch.object(bs, "get_by_url", new=mock.AsyncMock()) as m_modlix:
            hit = await bs.hydrate_from_storage("https://springs.com", {}, dict(self.CTX))
        self.assertFalse(hit)
        m_modlix.assert_not_awaited()
