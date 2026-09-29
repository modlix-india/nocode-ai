"""Unit: app/agents/adzump/services/product_service.py - save/resume and the
Modlix mirror record.

MySQL is the store of record (a failure raises), the Modlix mirror is
warn-only and never read back; `_build_full_record` projects the ds-v1
contract fields and keeps the competitive block honest.
"""

from __future__ import annotations

import inspect
import types
import unittest
from unittest import mock

from app.agents.adzump import stores
from app.agents.adzump.creative_intelligence.models import Creative
from app.agents.adzump.models.product import Product
from app.agents.adzump.services import product_service
from app.agents.adzump.services.product_service import (
    _build_location_object, _build_full_record,
)
from tests.agents.adzump._fixtures import RE

# Captured before any patch replaces them: _awaited_arg binds against these.
_INSERT_PRODUCT = stores.products.insert_product
_UPSERT_FLOW = stores.flows.upsert_flow
_SET_WEBSITE = stores.competitors.set_competitor_website
_MIRROR = product_service._mirror_modlix_record


def _awaited_arg(awaited: mock.AsyncMock, real_fn, name: str):
    """One named argument of a mock's last await, bound against the real
    signature so a reordered signature fails here instead of reading the
    wrong argument."""
    call = awaited.await_args
    return inspect.signature(real_fn).bind(*call.args, **call.kwargs).arguments[name]


class BuildLocationObjectLock(unittest.TestCase):

    def test_rows(self):
        # product.place is the one confirmed location: it wins over the typed
        # spec.location, which fills in only when place has no address.
        for label, place, location, coords in [
            ("a confirmed place wins, with coords",
             {"address": "Sarjapur Road, Bengaluru", "lat": 12.9, "lng": 77.7},
             "Sarjapur Road, Bengaluru", {"lng": 77.7, "lat": 12.9}),
            ("an address without coords still wins", {"address": "Hosur Road"},
             "Hosur Road", None),
            ("no address: the typed location fills in", {}, "Whitefield", None),
        ]:
            with self.subTest(label):
                out = _build_location_object({"location": "Whitefield"}, {"place": place})
                self.assertEqual(out["product_location"], location)
                self.assertEqual(out["product_coordinates"], coords)
                self.assertEqual(out["area_location"], "")


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
                # the classification adzump derived (taxonomy.py) for DS readers
                "category": "residential_apartment",
                "category_override": "residential_villa",
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
        self.assertEqual((record["category"], record["categoryOverride"]),
                         ("residential_apartment", "residential_villa"))
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


def _session(session: dict) -> dict:
    """A save mutates the competitor entries in place: copy them per test."""
    copy = dict(session)
    copy["competitor_analysis"] = {
        "competitors": [dict(c) for c in session["competitor_analysis"]["competitors"]]}
    return copy


def _row(row_id: int, name: str, status: str, total: int = 0) -> dict:
    return {"id": row_id, "name": name, "url": f"https://{name.lower()}.com",
            "url_source": "user", "logo_url": None, "business_type": "villas",
            "location": "Hebbal", "pricing": None, "key_usps": ["lake view"],
            "weakness": None, "why_competitor": "same buyer", "creative_status": status,
            "total_creatives": total, "active_creatives": total,
            "creatives_fetched_at": None}


class MySQLFirstPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """save_campaign writes only this chat's draft (a failure raises) and the
    warn-only Modlix mirror; shared data is never written from the chat's copy.
    hydrate_from_storage: MySQL only (the Modlix mirror is never read back)."""

    SESSION = {
        "product_profile": {"url": "https://springs.com",
                            "summary": "The rich SummaryAgent profile text."},
        "product_data": {"product_name": "Springs", "business_type": "real estate",
                         "summary": "villas"},
        "campaign_spec": {"platform": "Meta", "duration": "30 days"},
        "competitor_analysis": {"competitors": [{"name": "Sobha"}]},
    }
    CTX = {"client_code": "GRMEL", "auth": types.SimpleNamespace(user_id=7),
           "session_id": "sess-1"}

    def _patches(self, pid=42):
        return (
            mock.patch("app.agents.adzump.stores.products.product_id",
                       new=mock.AsyncMock(return_value=pid)),
            mock.patch("app.agents.adzump.stores.flows.upsert_flow",
                       new=mock.AsyncMock()),
            mock.patch("app.agents.adzump.services.product_service._mirror_modlix_record",
                       new=mock.AsyncMock(return_value="rec-1")),
        )

    async def test_only_the_draft_and_the_mirror(self):
        rows = [  # (case, product row id, draft written)
            ("product saved", 42, True),
            ("no product row yet: draft skipped", None, False),
        ]
        for case, pid, drafted in rows:
            p_pid, p_camp, p_mirror = self._patches(pid)
            with self.subTest(case), p_pid, p_camp as m_camp, p_mirror as m_mirror, \
                 mock.patch("app.agents.adzump.stores.products.update_product_fields") as m_fields, \
                 mock.patch("app.agents.adzump.stores.products.insert_product") as m_insert, \
                 mock.patch("app.agents.adzump.stores.competitors.add_competitor") as m_add, \
                 mock.patch("app.agents.adzump.stores.competitors.delete_competitor") as m_delete:
                result = await product_service.save_campaign(_session(self.SESSION), dict(self.CTX))
                self.assertEqual(result, "rec-1")
                for write in (m_fields, m_insert, m_add, m_delete):
                    write.assert_not_called()
                self.assertEqual(m_camp.await_count, int(drafted))
                if drafted:
                    self.assertEqual(_awaited_arg(m_camp, _UPSERT_FLOW, "session_id"), "sess-1")
                    self.assertEqual(_awaited_arg(m_camp, _UPSERT_FLOW, "user_id"), 7)
                    draft = _awaited_arg(m_camp, _UPSERT_FLOW, "data")
                    self.assertEqual(draft["platform"], "Meta")
                    self.assertNotIn("competitors", draft)  # the rows are the list's home
                mirror_record = _awaited_arg(m_mirror, _MIRROR, "record")
                self.assertNotIn("campaign", mirror_record)
                self.assertEqual(mirror_record["competitors"], [{"name": "Sobha"}])

    async def test_mysql_failure_raises_mirror_failure_does_not(self):
        p_pid, p_camp, p_mirror = self._patches()
        with p_pid, p_camp as m_camp, p_mirror:
            m_camp.side_effect = RuntimeError("db down")
            with self.assertRaises(RuntimeError):
                await product_service.save_campaign(_session(self.SESSION), dict(self.CTX))
        p_pid2, p_camp2, p_mirror2 = self._patches()
        with p_pid2, p_camp2, p_mirror2 as m_mirror:
            m_mirror.return_value = None  # mirror failed internally, warn-only
            result = await product_service.save_campaign(_session(self.SESSION), dict(self.CTX))
        self.assertIsNone(result)

    async def test_hydrate_mysql_hit_skips_modlix(self):
        product = Product(product_name="Springs", summary="villas",
                          profile_summary="The rich SummaryAgent profile text.")
        draft = {"location": {"address": "Hebbal, Bangalore"}}
        rows = [_row(1, "Sobha", "ok", total=1), _row(2, "Prestige", "pending"),
                _row(3, "Brigade", "error")]
        session_ctx: dict = {}
        with mock.patch("app.agents.adzump.stores.products.get_product",
                        new=mock.AsyncMock(return_value=product)), \
             mock.patch("app.agents.adzump.stores.products.product_id",
                        new=mock.AsyncMock(return_value=42)), \
             mock.patch("app.agents.adzump.stores.flows.latest_flow",
                        new=mock.AsyncMock(return_value=draft)), \
             mock.patch("app.agents.adzump.stores.competitors.list_product_competitors",
                        new=mock.AsyncMock(return_value=rows)), \
             mock.patch("app.agents.adzump.stores.competitors.list_product_creatives",
                        new=mock.AsyncMock(return_value={1: [Creative(creative_id="ad-1")]})), \
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
        # The list resumes from its rows: analyst notes and the website pin
        # survive, only a fetched row carries ads (the offer stays open for the
        # never-fetched and the failed one).
        sobha, prestige, brigade = session_ctx["competitor_analysis"]["competitors"]
        self.assertEqual((sobha["row_id"], sobha["why_competitor"], sobha["key_usps"],
                          sobha["url_source"]), (1, "same buyer", ["lake view"], "user"))
        self.assertEqual(([c["creativeId"] for c in sobha["creatives"]], sobha["totalCreatives"]),
                         (["ad-1"], 1))
        self.assertNotIn("creatives", prestige)
        self.assertNotIn("creatives", brigade)

    async def test_hydrate_mysql_miss_is_fresh_start(self):
        # MySQL is the ONLY hydration source: a miss returns False without
        # ever reading the Modlix mirror (write-only for DS).
        with mock.patch("app.agents.adzump.stores.products.get_product",
                        new=mock.AsyncMock(return_value=None)), \
             mock.patch.object(product_service, "get_by_url", new=mock.AsyncMock()) as m_modlix:
            hit = await product_service.hydrate_from_storage("https://springs.com", {}, dict(self.CTX))
        self.assertFalse(hit)
        m_modlix.assert_not_awaited()


class SaveCompetitorsTests(unittest.IsolatedAsyncioTestCase):
    """Each competitor change writes only its own row - never the chat's whole
    list, so a stale chat can't delete what another chat added."""

    SESSION = {"product_profile": {"url": "https://springs.com"}}
    ROW = {"id": 2, "url": "https://old.com/a", "url_source": ""}

    async def _save(self, entries, *, named_by_user=False, rows=(), pid=42,
                    added=9, moved=True):
        with mock.patch("app.agents.adzump.stores.products.product_id",
                        new=mock.AsyncMock(return_value=pid)), \
             mock.patch("app.agents.adzump.stores.competitors.list_product_competitors",
                        new=mock.AsyncMock(return_value=list(rows))) as m_rows, \
             mock.patch("app.agents.adzump.stores.competitors.add_competitor",
                        new=mock.AsyncMock(return_value=added)) as m_add, \
             mock.patch("app.agents.adzump.stores.competitors.fill_competitor_profile",
                        new=mock.AsyncMock()) as m_fill, \
             mock.patch("app.agents.adzump.stores.competitors.set_competitor_website",
                        new=mock.AsyncMock(return_value=moved)) as m_site:
            not_landed = await product_service.save_competitors(
                dict(self.SESSION), {"client_code": "GRMEL"}, entries,
                named_by_user=named_by_user)
        return not_landed, m_rows, m_add, m_fill, m_site

    async def test_each_entry_is_its_own_change(self):
        row = self.ROW
        rows = [  # (case, entry, kwargs, landed, added with revive, filled, website call)
            ("new research entry: added, never revived", {"name": "New"}, {},
             True, False, False, None),
            ("user named it: may revive a deleted row", {"name": "New"},
             {"named_by_user": True}, True, True, False, None),
            ("add didn't land", {"name": "New"}, {"added": None}, False, False, False, None),
            ("saved entry: fills its own row", {"name": "Old", "row_id": 2, "url": row["url"]},
             {"rows": [row]}, True, None, True, None),
            ("its row was removed since: nothing written", {"name": "Old", "row_id": 3},
             {"rows": [row]}, False, None, False, None),
            ("new website: the row moves there",
             {"name": "Old", "row_id": 2, "url": "https://new.com/a"},
             {"rows": [row]}, True, None, True, {"keep_user_pin": True}),
            ("user pin holds", {"name": "Old", "row_id": 2, "url": "https://new.com/a"},
             {"rows": [{**row, "url_source": "user"}]}, True, None, True, None),
        ]
        for case, entry, kwargs, landed, revive, filled, site in rows:
            with self.subTest(case):
                entry = dict(entry)
                not_landed, _, m_add, m_fill, m_site = await self._save([entry], **kwargs)
                self.assertEqual(not_landed == [], landed)
                if revive is None:
                    m_add.assert_not_awaited()
                else:
                    self.assertEqual(m_add.await_args.kwargs["revive"], revive)
                self.assertEqual(m_fill.await_count, int(filled))
                if site is None:
                    m_site.assert_not_awaited()
                else:
                    self.assertEqual(
                        {k: m_site.await_args.kwargs[k] for k in site}, site)

    async def test_new_entry_takes_its_row_id(self):
        entry = {"name": "New"}
        await self._save([entry])
        self.assertEqual(entry["row_id"], 9)

    async def test_website_taken_keeps_the_rows_own(self):
        entry = {"name": "Old", "row_id": 2, "url": "https://new.com/a"}
        await self._save([entry], rows=[self.ROW], moved=False)
        self.assertEqual(entry["url"], self.ROW["url"])

    async def test_no_product_row_writes_nothing(self):
        not_landed, m_rows, m_add, _, _ = await self._save([{"name": "New"}], pid=None)
        self.assertEqual(not_landed, [])
        m_rows.assert_not_awaited()
        m_add.assert_not_awaited()


class RemoveAndPinTests(unittest.IsolatedAsyncioTestCase):
    SESSION = {"product_profile": {"url": "https://springs.com"}}
    CTX = {"client_code": "GRMEL"}

    def _patch_pid(self):
        return mock.patch("app.agents.adzump.stores.products.product_id",
                          new=mock.AsyncMock(return_value=42))

    async def test_remove_marks_only_saved_rows(self):
        with self._patch_pid(), mock.patch(
                "app.agents.adzump.stores.competitors.delete_competitor",
                new=mock.AsyncMock(return_value=True)) as m_delete:
            await product_service.remove_competitors(
                dict(self.SESSION), self.CTX, [{"name": "Saved", "row_id": 2}, {"name": "Unsaved"}])
        m_delete.assert_awaited_once_with("GRMEL", 42, 2, 0)

    async def test_pin(self):
        rows = [  # (case, entry, website moved, refusal contains, website written)
            ("pinned", {"name": "Sobha", "row_id": 2}, True, "", True),
            ("website taken", {"name": "Sobha", "row_id": 2}, False, "already belongs", True),
            ("not saved yet", {"name": "Sobha"}, True, "isn't saved yet", False),
        ]
        for case, entry, moved, refusal, written in rows:
            with self.subTest(case), self._patch_pid(), mock.patch(
                    "app.agents.adzump.stores.competitors.set_competitor_website",
                    new=mock.AsyncMock(return_value=moved)) as m_site:
                result = await product_service.pin_competitor_website(
                    dict(self.SESSION), self.CTX, entry, "https://sobha.com/lake")
                self.assertIn(refusal, result) if refusal else self.assertEqual(result, "")
                self.assertEqual(m_site.await_count, int(written))
                if written:
                    call = inspect.signature(_SET_WEBSITE).bind(
                        *m_site.await_args.args, **m_site.await_args.kwargs).arguments
                    self.assertEqual((call["url_source"], call["keep_user_pin"]),
                                     ("user", False))


class ProductWritesTests(unittest.IsolatedAsyncioTestCase):
    """Each product change writes only its own fields; analysis creates the
    row."""

    SESSION = {"product_profile": {"url": "https://springs.com", "summary": "Display profile"}}
    CTX = {"client_code": "GRMEL", "auth": types.SimpleNamespace(user_id=7)}

    def _session(self, **product):
        return {**self.SESSION, "product_data": {"product_name": "Springs", **product}}

    async def test_save_product_fields(self):
        meta, google = {"account": "act_1"}, {"account": "111"}
        rows = [  # (case, fields, row id, db error, saved, copy after)
            ("one field", {"place": {"address": "Hebbal"}}, 42, False, True,
             {"place": {"address": "Hebbal"}, "ad_accounts": {"google": google}}),
            ("one platform's accounts", {"ad_accounts.meta": meta}, 42, False, True,
             {"place": {}, "ad_accounts": {"google": google, "meta": meta}}),
            ("no product row: the copy still takes it", {"ad_accounts.meta": meta}, None,
             False, False, {"place": {}, "ad_accounts": {"google": google, "meta": meta}}),
            ("database error: raises, copy untouched", {"place": {"address": "Hebbal"}}, 42,
             True, None, {"place": {}, "ad_accounts": {"google": google}}),
        ]
        for case, fields, pid, error, saved, after in rows:
            with self.subTest(case):
                session = self._session(place={}, ad_accounts={"google": dict(google)})
                update = mock.AsyncMock(side_effect=RuntimeError("down") if error else None,
                                        return_value=True)
                with mock.patch("app.agents.adzump.stores.products.product_id",
                                new=mock.AsyncMock(return_value=pid)), \
                     mock.patch("app.agents.adzump.stores.products.update_product_fields",
                                new=update):
                    if error:
                        with self.assertRaises(RuntimeError):
                            await product_service.save_product_fields(session, self.CTX, fields)
                    else:
                        result = await product_service.save_product_fields(session, self.CTX, fields)
                        self.assertEqual(result, saved)
                for key, value in after.items():
                    self.assertEqual(session["product_data"][key], value)
                if pid and not error:
                    self.assertEqual(update.await_args.args[2], fields)

    async def test_analysis_creates_the_row_once(self):
        stored = Product(product_name="Springs (saved)", profile_summary="Display profile")
        session = self._session(summary="villas")
        with mock.patch("app.agents.adzump.stores.products.insert_product",
                        new=mock.AsyncMock(return_value=42)) as m_insert, \
             mock.patch("app.agents.adzump.stores.products.get_product",
                        new=mock.AsyncMock(return_value=stored)):
            pid = await product_service.save_analyzed_product(session, self.CTX)
        self.assertEqual(pid, 42)
        inserted = _awaited_arg(m_insert, _INSERT_PRODUCT, "product")
        self.assertEqual((inserted.profile_summary, inserted.summary), ("Display profile", "villas"))
        self.assertEqual(_awaited_arg(m_insert, _INSERT_PRODUCT, "user_id"), 7)
        # An existing row wins: the copy becomes the stored product.
        self.assertEqual(session["product_data"]["product_name"], "Springs (saved)")


class ReloadCompetitorListTests(unittest.IsolatedAsyncioTestCase):
    """After the chat's own remove or website pin, the list is re-read from its
    rows (unsaved entries saved first)."""

    CTX = {"client_code": "GRMEL"}

    async def _refresh(self, entries, rows, ads, pid=42):
        session = {"product_profile": {"url": "https://springs.com"},
                   "competitor_analysis": {"competitors": entries}}
        with mock.patch("app.agents.adzump.stores.products.product_id",
                        new=mock.AsyncMock(return_value=pid)), \
             mock.patch("app.agents.adzump.stores.competitors.list_product_competitors",
                        new=mock.AsyncMock(return_value=rows)), \
             mock.patch("app.agents.adzump.stores.competitors.list_product_creatives",
                        new=mock.AsyncMock(return_value=ads)), \
             mock.patch("app.agents.adzump.stores.competitors.add_competitor",
                        new=mock.AsyncMock(return_value=4)) as self.m_add:
            changed = await product_service.reload_competitor_list(
                session["competitor_analysis"], session, self.CTX)
        return changed, session["competitor_analysis"]["competitors"]

    async def test_rows_are_the_list(self):
        sobha = product_service._competitor_entry(_row(1, "Sobha", "pending"), [])
        shriram = product_service._competitor_entry(
            _row(2, "Shriram", "ok", total=1), [Creative(creative_id="ad-1")])
        unsaved = {"name": "Brigade"}
        rows = [  # (label, chat entries, rows, ads, changed, names after)
            ("unchanged list: no repaint", [sobha, shriram],
             [_row(1, "Sobha", "pending"), _row(2, "Shriram", "ok", total=1)],
             {2: [Creative(creative_id="ad-1")]}, False, ["Sobha", "Shriram"]),
            ("a row gone: its entry dropped", [sobha, shriram],
             [_row(2, "Shriram", "ok", total=1)],
             {2: [Creative(creative_id="ad-1")]}, True, ["Shriram"]),
            ("an ad hidden: its ad goes", [sobha, shriram],
             [_row(1, "Sobha", "pending"), _row(2, "Shriram", "ok")],
             {}, True, ["Sobha", "Shriram"]),
            ("a new row: adopted", [sobha],
             [_row(1, "Sobha", "pending"), _row(3, "Prestige", "pending")],
             {}, True, ["Sobha", "Prestige"]),
            ("an unsaved entry is saved first, then read back", [sobha, unsaved],
             [_row(1, "Sobha", "pending"), _row(4, "Brigade", "pending")],
             {}, False, ["Sobha", "Brigade"]),
        ]
        for label, entries, stored, ads, changed, names in rows:
            with self.subTest(label):
                result, after = await self._refresh([dict(e) for e in entries], stored, ads)
                self.assertEqual((result, [c["name"] for c in after]), (changed, names))
        _, after = await self._refresh([dict(sobha), dict(shriram)],
                                       [_row(1, "Sobha", "pending"), _row(2, "Shriram", "ok")], {})
        self.assertEqual(after[1]["creatives"], [])

    async def test_no_product_row_keeps_the_list(self):
        changed, after = await self._refresh([{"name": "Brigade"}], [], {}, pid=None)
        self.assertEqual((changed, after), (False, [{"name": "Brigade"}]))
        self.m_add.assert_not_awaited()


class DropDeletedCompetitorsTests(unittest.IsolatedAsyncioTestCase):
    async def test_research_never_re_suggests_a_deleted_competitor(self):
        deleted = [{"name": "Sobha Lake Gardens", "url": "https://sobha.com/lake-gardens"},
                   {"name": "Nambiar Villas", "url": None}]
        fresh = [{"name": "Sobha Lake Gardens (Phase 2)", "url": "https://www.sobha.com/lake-gardens/"},
                 {"name": "nambiar villas", "url": None},
                 {"name": "Prestige Lakeside", "url": "https://prestige.com/lakeside"}]
        ctx = {"client_code": "GRMEL",
               "session_context": {"product_profile": {"url": "https://springs.com"}}}
        competitive = {"competitors": [dict(c) for c in fresh]}
        with mock.patch("app.agents.adzump.stores.products.product_id",
                        new=mock.AsyncMock(return_value=42)), \
             mock.patch("app.agents.adzump.stores.competitors.deleted_competitors",
                        new=mock.AsyncMock(return_value=deleted)):
            left_out = await product_service.drop_deleted_competitors(competitive, ctx)
        # Matched by website (any spelling) or by name (any case).
        self.assertEqual(left_out, ["Sobha Lake Gardens (Phase 2)", "nambiar villas"])
        self.assertEqual([c["name"] for c in competitive["competitors"]], ["Prestige Lakeside"])


if __name__ == "__main__":
    unittest.main()
