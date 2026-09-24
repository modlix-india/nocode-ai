"""Unit: app/agents/adzump/services/product_service.py - save/resume and the
Modlix mirror record.

MySQL is the store of record (a failure raises), the Modlix mirror is
warn-only and never read back; `_build_full_record` projects the ds-v1
contract fields and keeps the competitive block honest.
"""

from __future__ import annotations

import inspect
import unittest
from unittest import mock

from app.agents.adzump import stores
from app.agents.adzump.models.product import Product
from app.agents.adzump.services import product_service
from app.agents.adzump.services.product_service import (
    _build_location_object, _build_full_record,
)
from tests.agents.adzump._fixtures import RE

# Captured before any patch replaces them: _awaited_arg binds against these.
_UPSERT_PRODUCT = stores.products.upsert_product
_UPSERT_FLOW = stores.flows.upsert_flow
_SYNC_PROFILES = stores.competitors.sync_competitor_profiles
_MIRROR = product_service._mirror_modlix_record


def _awaited_arg(awaited: mock.AsyncMock, real_fn, name: str):
    """One named argument of a mock's last await, bound against the real
    signature so a reordered signature fails here instead of reading the
    wrong argument."""
    call = awaited.await_args
    return inspect.signature(real_fn).bind(*call.args, **call.kwargs).arguments[name]


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
    def test_session_id_is_the_one_save_campaign_passes(self):
        # regression: PR #91 B7 - read off session context (zero writers), always empty.
        sc = {"product_data": dict(RE), "campaign_spec": {"platform": "Google Ads"}}
        for chat_session_id in ("adzump-C1-42", ""):
            with self.subTest(chat_session_id=chat_session_id):
                record = _build_full_record(sc, "https://example.com", chat_session_id)
                self.assertEqual(record["campaign"]["sessionId"], chat_session_id)


class LaunchRecordTests(unittest.TestCase):
    def test_competitive_block_stays_honest(self):
        # regression: F26 (decline then reverse) - an analysis that ran is never "declined".
        declined_flag = {"platform": "Google Ads", "competitive_analysis_declined": "true"}
        for name, spec, competitors, attempted, declined in [
            ("ran after a stale decline", declined_flag,
             [{"name": "Prestige"}, {"name": "Brigade"}], True, False),
            ("ran after a stale decline, found nothing", declined_flag, [], True, False),
            ("never ran, declined", declined_flag, None, False, True),
            ("enum decline matches the legacy flag",
             {"platform": "Google Ads", "competitive_analysis": "declined"}, None, False, True),
            ("neither", {"platform": "Google Ads"}, None, False, False),
        ]:
            with self.subTest(name):
                c = _rec(spec, competitors=competitors)
                self.assertEqual((c["attempted"], c["declined"]), (attempted, declined))


class MirrorRecordProjectionTests(unittest.TestCase):
    """product_data → _build_full_record: the ds-side contract fields."""

    def test_mirror_record_projects_ds_contract(self):
        session_ctx = {
            "product_data": {
                "product_name": "Sumadhura Solea",
                "business_type": "real estate",
                "business_scale": "local",
                "summary": "Luxury 3 & 4 BHK apartments.",
                "primary_url": "https://dahliasgurgaon.com/",
                "pages": {"https://dahliasgurgaon.com/":
                          {"screenshot_url": "https://cdn/x.png"}},
                "assets": {
                    "logos": [{"url": "https://cdn/logo.png", "source": "scrape",
                               "confidence": 0.9}],
                    "images": [{"url": "https://cdn/c1.png",
                                "display": {"fit": "cover"},
                                "role": "hero", "source": "site_pick"}],
                },
                "target_areas": [{"name": "Whitefield", "lat": 12.96, "lng": 77.75,
                                  "distance_km": 5.0,
                                  "meta": {"type": "city", "key": "777",
                                           "name": "Whitefield"}}],
                "place": {"address": "Bengaluru", "lat": 12.96, "lng": 77.75,
                          "country_code": "IN",
                          "country_geo_constant": "geoTargetConstants/2356",
                          "display_name": "Sumadhura Solea, Bengaluru"},
            },
            "campaign_spec": {"platform": "Meta", "location": "Bengaluru"},
        }

        record = _build_full_record(session_ctx, "https://dahliasgurgaon.com/")

        self.assertEqual(record["productName"], "Sumadhura Solea")
        self.assertEqual(record["screenshot"], "https://cdn/x.png")
        self.assertEqual(record["logoUrl"], "https://cdn/logo.png")
        self.assertEqual(record["creativeImages"], ["https://cdn/c1.png"])
        # The per-platform mapped-location key (the original bug) must project.
        self.assertEqual(
            record["campaign"]["metaMappedLocations"][0]["meta"]["key"], "777")
        self.assertEqual(record["campaign"]["googleMappedLocations"], [])
        # country_code + geo constant (scope geo lookups) must project.
        self.assertEqual(record["campaign"]["location"]["country_code"], "IN")
        self.assertEqual(
            record["campaign"]["location"]["country_geo_constant"],
            "geoTargetConstants/2356")


class MySQLFirstPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """save_campaign: MySQL is the store of record, Modlix a warn-only mirror
    without the campaign sub-object. hydrate_from_storage: MySQL first, legacy
    MySQL miss is a fresh start (the Modlix mirror is never read back)."""

    SESSION = {
        "product_profile": {"url": "https://springs.com",
                            "summary": "The rich SummaryAgent profile text."},
        "product_data": {"product_name": "Springs", "business_type": "real estate",
                         "summary": "villas"},
        "campaign_spec": {"platform": "Meta", "duration": "30 days"},
        "competitor_analysis": {"competitors": [{"name": "Sobha"}]},
        "_session_id": "sess-1",
    }
    CTX = {"client_code": "GRMEL", "user_id": 7, "session_id": "sess-1"}

    def _patches(self):
        return (
            mock.patch("app.agents.adzump.stores.products.upsert_product",
                       new=mock.AsyncMock(return_value=42)),
            mock.patch("app.agents.adzump.stores.flows.upsert_flow",
                       new=mock.AsyncMock()),
            mock.patch("app.agents.adzump.stores.competitors.sync_competitor_profiles",
                       new=mock.AsyncMock()),
            mock.patch("app.agents.adzump.services.product_service._mirror_modlix_record",
                       new=mock.AsyncMock(return_value="rec-1")),
        )

    async def test_mysql_written_then_mirror_without_campaign(self):
        p_prod, p_camp, p_profiles, p_mirror = self._patches()
        with p_prod as m_prod, p_camp as m_camp, \
             p_profiles as m_profiles, p_mirror as m_mirror:
            result = await product_service.save_campaign(dict(self.SESSION), dict(self.CTX))
        self.assertEqual(result, "rec-1")
        m_prod.assert_awaited_once()
        # The display profile persists on the typed Product; the machine brief
        # (product_data.summary) stays its own field.
        saved_product = _awaited_arg(m_prod, _UPSERT_PRODUCT, "product")
        self.assertEqual(saved_product.profile_summary,
                         "The rich SummaryAgent profile text.")
        self.assertEqual(saved_product.summary, "villas")
        draft = _awaited_arg(m_camp, _UPSERT_FLOW, "data")
        self.assertEqual(draft["platform"], "Meta")
        self.assertEqual(draft["competitors"], [{"name": "Sobha"}])
        # Curated competitors get profile rows at save time, not fetch time.
        self.assertEqual(_awaited_arg(m_profiles, _SYNC_PROFILES, "competitors"),
                         [{"name": "Sobha"}])
        mirror_record = _awaited_arg(m_mirror, _MIRROR, "record")
        self.assertNotIn("campaign", mirror_record)

    async def test_mysql_failure_raises_mirror_failure_does_not(self):
        p_prod, p_camp, p_profiles, p_mirror = self._patches()
        with p_prod, p_camp as m_camp, p_profiles, p_mirror:
            m_camp.side_effect = RuntimeError("db down")
            with self.assertRaises(RuntimeError):
                await product_service.save_campaign(dict(self.SESSION), dict(self.CTX))
        p_prod2, p_camp2, p_profiles2, p_mirror2 = self._patches()
        with p_prod2, p_camp2, p_profiles2, p_mirror2 as m_mirror:
            m_mirror.return_value = None  # mirror failed internally, warn-only
            result = await product_service.save_campaign(dict(self.SESSION), dict(self.CTX))
        self.assertIsNone(result)

    async def test_hydrate_mysql_hit_skips_modlix(self):
        product = Product(product_name="Springs", summary="villas",
                          profile_summary="The rich SummaryAgent profile text.")
        draft = {"location": {"address": "Hebbal, Bangalore"},
                 "competitors": [{"name": "Sobha"}]}
        session_ctx: dict = {}
        with mock.patch("app.agents.adzump.stores.products.get_product",
                        new=mock.AsyncMock(return_value=product)), \
             mock.patch("app.agents.adzump.stores.products.product_id",
                        new=mock.AsyncMock(return_value=42)), \
             mock.patch("app.agents.adzump.stores.flows.latest_flow",
                        new=mock.AsyncMock(return_value=draft)), \
             mock.patch.object(product_service, "get_by_url",
                               new=mock.AsyncMock()) as m_modlix:
            hit = await product_service.hydrate_from_storage("https://springs.com", session_ctx, dict(self.CTX))
        self.assertTrue(hit)
        m_modlix.assert_not_awaited()
        self.assertEqual(session_ctx["product_data"]["product_name"], "Springs")
        # The panel resumes with the display profile, not the machine brief.
        self.assertEqual(session_ctx["product_profile"]["summary"],
                         "The rich SummaryAgent profile text.")
        self.assertEqual(session_ctx["campaign_spec"]["location"], "Hebbal, Bangalore")
        self.assertEqual(session_ctx["competitor_analysis"]["competitors"], [{"name": "Sobha"}])

    async def test_hydrate_mysql_miss_is_fresh_start(self):
        # MySQL is the ONLY hydration source: a miss returns False without
        # ever reading the Modlix mirror (write-only for DS).
        with mock.patch("app.agents.adzump.stores.products.get_product",
                        new=mock.AsyncMock(return_value=None)), \
             mock.patch.object(product_service, "get_by_url", new=mock.AsyncMock()) as m_modlix:
            hit = await product_service.hydrate_from_storage("https://springs.com", {}, dict(self.CTX))
        self.assertFalse(hit)
        m_modlix.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
