"""tools/competitor.py helpers: the competitor_id + official_url_id -> URL
join (the researcher owns URL judgment; code holds custody), user URL pins
(verify + apply), and refresh/merge behavior."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from app.agents.adzump.tools import competitor
from app.agents.adzump.tools.competitor import (
    _apply_url_updates,
    _join_verified_urls,
    _verify_competitor_url,
)


class JoinVerifiedUrlsTests(unittest.TestCase):
    """The analyst cites evidence by competitor_id and judges the official URL
    by official_url_id; code attaches the custody-held URLs. A model-written
    url is discarded when the ID resolves; a deliberate null pick means
    honestly link-less; unknown picks fall back to the fetched page."""

    SESSION = {"_research_state": {"verified_competitors": [
        {"cid": "C1", "name": "Sobha Magnus",
         "url": "https://propsoch.com/sobha-magnus",
         "fetch_url": "https://www.sobha.com/sobha-magnus/",
         "url_options": {"C1.U1": "https://www.sobha.com/sobha-magnus/",
                         "C1.U2": "https://www.sobha.com/"}},
        {"cid": "C2", "name": "Lodha Azur", "url": "https://lodhagroup.com/azur"},
        {"cid": "C3", "name": "Clone Co",
         "fetch_url": "https://cloneco.co.in/",  # vetting refused this host
         "url_options": {}},
    ]}}

    def test_join_rows(self):
        rows = [
            ("analyst's pick wins, model url discarded",
             {"name": "Sobha Magnus", "competitor_id": "C1",
              "official_url_id": "C1.U1",
              "url": "https://sobha-magnus-typo.com"},
             "https://www.sobha.com/sobha-magnus/"),
            ("deliberate null pick means link-less",
             {"name": "Sobha Magnus", "competitor_id": "C1",
              "official_url_id": None, "url": "https://typed.example"},
             None),
            ("unknown pick falls back to the fetched page",
             {"name": "Sobha Magnus", "competitor_id": "C1",
              "official_url_id": "C1.U9", "url": None},
             "https://www.sobha.com/sobha-magnus/"),
            ("no pick emitted (old shape): fetched page, as before",
             {"name": "Sobha Magnus", "competitor_id": "C1", "url": None},
             "https://www.sobha.com/sobha-magnus/"),
            ("no fetch_url falls back to verified url",
             {"name": "Lodha Azur", "competitor_id": "C2", "url": None},
             "https://lodhagroup.com/azur"),
            ("unknown id keeps model url",
             {"name": "Ghost", "competitor_id": "C9",
              "url": "https://ghost.example"},
             "https://ghost.example"),
            ("no id keeps entry untouched (add-by-name shape)",
             {"name": "Manual Add", "url": "https://manual.example"},
             "https://manual.example"),
            # The fallback is VETTED: a broker-style fetch host the options
            # gathering refused must never ship via the old-shape/unknown-pick
            # back door - link-less beats a refused host.
            ("old-shape fallback never ships a refused host",
             {"name": "Clone Co", "competitor_id": "C3", "url": None},
             None),
            ("unknown pick on refused host stays link-less",
             {"name": "Clone Co", "competitor_id": "C3",
              "official_url_id": "C3.U9", "url": None},
             None),
            # Name-mismatch guard: an entry citing a candidate whose host and
            # title share NO brand token gets its URL dropped (live 2026-09-10:
            # 'Purva Symphony' cited the Valmark Cityville candidate, and the
            # wrong domain key hijacked Valmark's ad search).
            ("cross-brand citation is dropped",
             {"name": "Purva Symphony", "competitor_id": "C1",
              "official_url_id": "C1.U1", "url": None},
             None),
            ("generic-only name never false-flags",
             {"name": "Luxury Villas", "competitor_id": "C2", "url": None},
             "https://lodhagroup.com/azur"),
        ]
        for label, comp, expected_url in rows:
            with self.subTest(label):
                competitive = {"competitors": [comp]}
                _join_verified_urls(competitive, dict(self.SESSION))
                self.assertEqual(comp.get("url"), expected_url)
                self.assertNotIn("competitor_id", comp)   # transport keys
                self.assertNotIn("official_url_id", comp)  # stripped

    def test_empty_state_is_noop(self):
        comp = {"name": "X", "competitor_id": "C1", "url": "https://x.example"}
        _join_verified_urls({"competitors": [comp]}, {})
        self.assertEqual(comp["url"], "https://x.example")


class VerifyCompetitorUrlTests(unittest.TestCase):
    """A user URL becomes the entry's identity only if reachable, non-portal,
    and actually about the competitor; every rejection carries a user-facing
    reason."""

    def _verify(self, name, url, *, answer=None, final_url=None):
        async def fake_fetch(u, question):
            if answer is None:
                raise ConnectionError("dead")
            return {"status": "ok", "answer": answer,
                    "url": final_url or u, "title": "t"}
        with mock.patch(
            "app.agents.adzump.agents.product.adapters"
            ".web_fetch_adapter.fetch_and_answer",
            new=fake_fetch,
        ):
            return asyncio.run(_verify_competitor_url(name, url))

    def test_rows(self):
        rows = [
            ("match accepted, post-redirect url kept",
             "https://nambiarprojects.com/x",
             dict(answer="MATCH: YES\nOfficial project page.",
                  final_url="https://nambiarprojects.com/nambiar-bannerghatta-road/"),
             "https://nambiarprojects.com/nambiar-bannerghatta-road/", False),
            # the user is the authority: a live non-portal pin stands, with a caution
            ("content mismatch accepted with caution",
             "https://sobha.com/other",
             dict(answer="MATCH: NO\nA different Sobha project page."),
             "https://sobha.com/other", True),
            ("dead site rejected", "https://deadclone.co.in/",
             dict(), "", True),
            ("portal rejected without a fetch", "https://99acres.com/x",
             dict(answer="MATCH: YES"), "", True),
            ("redirect into a portal rejected", "https://short.link/x",
             dict(answer="MATCH: YES", final_url="https://99acres.com/y"),
             "", True),
            ("garbage url rejected", "not-a-url", dict(answer="MATCH: YES"),
             "", True),
        ]
        for label, url, kwargs, expected_url, has_reason in rows:
            with self.subTest(label):
                verified, reason = self._verify(
                    "Nambiar Villas Bannerghatta", url,
                    answer=kwargs.get("answer"), final_url=kwargs.get("final_url"))
                self.assertEqual(verified, expected_url)
                self.assertEqual(bool(reason), has_reason)


class ApplyUrlUpdatesTests(unittest.TestCase):
    def _apply(self, set_url, competitors, verified=("https://ok.example/", ""), refusal=""):
        competitive = {"competitors": competitors}
        with mock.patch.object(
            competitor, "_verify_competitor_url",
            new=mock.AsyncMock(return_value=verified),
        ), mock.patch(
            "app.agents.adzump.services.product_service.pin_competitor_website",
            new=mock.AsyncMock(return_value=refusal),
        ):
            acks, rejections = asyncio.run(
                _apply_url_updates(set_url, competitive, {}, {}))
        return acks, rejections

    def test_pin_updates_entry_and_keeps_its_ads(self):
        # Kailash 2026-09-29: the ad search runs on the name, so a new website
        # never costs a new search (live 2026-09-28: Sobha Magnus searched twice).
        entry = {"name": "Nambiar Villas", "url": "https://clone.co.in",
                 "creatives": [{"creativeId": "a"}], "totalCreatives": 1,
                 "activeCreatives": 1}
        acks, rejections = self._apply("Nambiar | https://real.example", [entry])
        self.assertEqual(len(acks), 1)
        self.assertEqual(rejections, [])
        self.assertEqual(entry["url"], "https://ok.example/")
        self.assertEqual(entry["url_source"], "user")
        self.assertEqual(entry["creatives"], [{"creativeId": "a"}])

    def test_rejected_pins_never_apply(self):
        rows = [
            ("verification failed", "Nambiar | https://wrong.example",
             [{"name": "Nambiar Villas", "url": None}], ("", "that page is a broker site")),
            ("unknown competitor", "Ghost | https://x.example",
             [{"name": "Nambiar Villas"}], ("https://x.example", "")),
            ("unparseable spec", "just some text",
             [{"name": "Nambiar Villas"}], ("https://x.example", "")),
            ("website already another competitor's", "Nambiar | https://x.example",
             [{"name": "Nambiar Villas"}], ("https://x.example", ""), "already belongs"),
        ]
        for label, spec, competitors, verified, *refusal in rows:
            with self.subTest(label):
                acks, rejections = self._apply(spec, competitors, verified, *refusal)
                self.assertEqual(acks, [])
                self.assertEqual(len(rejections), 1)
                self.assertNotEqual(competitors[0].get("url_source"), "user")


class SameProjectTests(unittest.TestCase):
    """A looked-up name refreshes its entry; sibling projects stay separate (live 2026-09-08)."""

    def test_rows(self):
        from app.agents.adzump.tools.competitor import _same_project
        rows = [
            ("word inserted", "Nambiar Villas",
             "Nambiar Bannerghatta Villas", True),
            ("exact", "Sobha Magnus", "Sobha Magnus", True),
            ("spacing/case", "Purva Sparkling Springs",
             "PURVA SparklingSprings", True),
            ("parenthetical gloss", "Nambiar Villas",
             "Nambiar Villas (Nambiar Bannerghatta Villas)", True),
            ("sibling projects stay separate", "Purva Sparkling Springs",
             "Purva Sound of Water", False),
            ("different brands", "Sobha Magnus", "Lodha Azur", False),
            ("empty", "", "Sobha Magnus", False),
        ]
        for label, a, b, expected in rows:
            with self.subTest(label):
                self.assertIs(_same_project(a, b), expected)
                self.assertIs(_same_project(b, a), expected)


class RefreshEntryTests(unittest.TestCase):
    def test_rows(self):
        from app.agents.adzump.tools.competitor import _refresh_entry
        rows = [
            ("fills gaps, keeps set fields",
             {"name": "Nambiar Villas", "location": "Bannerghatta"},
             {"name": "Nambiar Bannerghatta Villas", "location": "elsewhere",
              "pricing": "₹3-5 Cr"},
             {"location": "Bannerghatta", "pricing": "₹3-5 Cr"}),
            ("user pin never overwritten",
             {"name": "Nambiar Villas", "url": "https://pinned.example",
              "url_source": "user"},
             {"name": "Nambiar Villas", "url": "https://other.example"},
             {"url": "https://pinned.example"}),
            ("new host adopts url",
             {"name": "Nambiar Villas", "url": None},
             {"name": "Nambiar Villas", "url": "https://nambiarprojects.com/x"},
             {"url": "https://nambiarprojects.com/x"}),
        ]
        for label, existing, fresh, expected in rows:
            with self.subTest(label):
                _refresh_entry(existing, fresh)
                for key, value in expected.items():
                    self.assertEqual(existing.get(key), value)

    def test_a_new_website_keeps_the_ads(self):
        # The ad search runs on the name: a new site, even a new host, never
        # drops the entry's ads (Kailash 2026-09-29).
        from app.agents.adzump.tools.competitor import _refresh_entry
        for label, new_url in [("new host", "https://new.example/"),
                               ("same host", "https://old.example/page")]:
            with self.subTest(label):
                entry = {"name": "N", "url": "https://old.example/",
                         "creatives": [{"creativeId": "a"}], "totalCreatives": 1}
                _refresh_entry(entry, {"name": "N", "url": new_url})
                self.assertEqual(entry["url"], new_url)
                self.assertEqual(entry["creatives"], [{"creativeId": "a"}])


class AnalyzeReentrancyAndMergeTests(unittest.TestCase):
    """One analyst at a time; re-discovery merges, never replaces (live 2026-09-08)."""

    def _context(self, competitors=None):
        session_ctx = {
            "product_data": {"product_name": "Valmark CityVille",
                             "summary": "Luxury villaments"},
            "product_profile": {"summary": "Luxury villaments",
                                "url": "https://cityville.in"},
        }
        if competitors is not None:
            session_ctx["competitor_analysis"] = {"competitors": competitors}
        return {"session_context": session_ctx, "auth": object(),
                "event_stream": None, "tool_use_id": "t1"}

    def test_second_concurrent_call_refuses(self):
        from app.agents.adzump.tools.competitor import _analyze_competitors
        context = self._context()
        started = asyncio.Event()

        async def slow_impl(params, ctx):
            started.set()
            await asyncio.sleep(0.05)
            return competitor.ToolResult(success=True, summary="done")

        async def race():
            with mock.patch.object(competitor, "_analyze_competitors_impl",
                                   new=slow_impl):
                first = asyncio.create_task(_analyze_competitors({}, context))
                await started.wait()
                second = await _analyze_competitors({}, context)
                return await first, second

        first, second = asyncio.run(race())
        self.assertTrue(first.success)
        self.assertFalse(second.success)
        # The lock releases - a later call is welcome again.
        self.assertNotIn("_competitor_analysis_running",
                         context["session_context"])

    def test_force_rediscovery_merges_never_replaces(self):
        from app.agents.adzump.tools.competitor import _analyze_competitors
        pinned = {"name": "Nambiar Villas", "url": "https://pinned.example",
                  "url_source": "user", "creatives": [{"creativeId": "a"}]}
        context = self._context(competitors=[pinned])
        fresh = {"competitors": [
            {"name": "Nambiar Bannerghatta Villas",
             "url": "https://clone.example"},   # same project -> refresh, pin wins
            {"name": "Sobha Magnus", "url": "https://sobha.com/sobha-magnus"},
        ]}
        analyst = mock.Mock()
        analyst.analyze = mock.AsyncMock(return_value=SimpleNamespace(
            competitive=fresh, product=None, notes=[]))
        with mock.patch(
            "app.agents.adzump.agents.product.agent.get_product_agent",
            return_value=analyst,
        ), mock.patch.object(
            competitor, "_filter_self_references",
        ), mock.patch(
            "app.core.streaming.pre_emit_agent_started", new=mock.AsyncMock(),
        ), mock.patch(
            "app.agents.adzump.services.product_service.drop_deleted_competitors",
            new=mock.AsyncMock(return_value=[]),
        ), mock.patch(
            "app.agents.adzump.services.product_service.save_competitors",
            new=mock.AsyncMock(return_value=[]),
        ) as m_save, mock.patch(
            "app.agents.adzump.services.product_service.reload_competitor_list",
            new=mock.AsyncMock(),
        ):
            result = asyncio.run(
                _analyze_competitors({"force": "true"}, context))
        self.assertTrue(result.success)
        # What it found and what it refreshed are saved as research, never as the user's pick.
        self.assertEqual([c["name"] for c in m_save.await_args.args[2]],
                         ["Sobha Magnus", "Nambiar Villas"])
        self.assertFalse(m_save.await_args.kwargs["named_by_user"])
        names = [c["name"] for c in
                 context["session_context"]["competitor_analysis"]["competitors"]]
        self.assertEqual(names, ["Nambiar Villas", "Sobha Magnus"])
        self.assertEqual(pinned["url"], "https://pinned.example")  # pin survived
        self.assertEqual(pinned["creatives"], [{"creativeId": "a"}])
        self.assertIn("1 new", result.summary)
        self.assertIn("1 already known", result.summary)


class CompetitorWritesTests(unittest.TestCase):
    """Each change writes only its own rows; a chat that never loaded the list
    edits the saved one, never an empty copy (which used to delete every row)."""

    SAVED = [{"name": "Sobha Magnus", "row_id": 1}, {"name": "Nambiar Villas", "row_id": 2}]

    def _run(self, params, *, saved=SAVED, found=None, not_landed=(), save_error=False):
        session_ctx = {"product_data": {"product_name": "Valmark CityVille"},
                       "product_profile": {"url": "https://cityville.in"}}
        context = {"session_context": session_ctx, "auth": object(),
                   "event_stream": None, "tool_use_id": "t1"}
        analyst = mock.Mock()
        analyst.analyze = mock.AsyncMock(return_value=SimpleNamespace(
            competitive={"competitors": found or [], "skipped": []}, product=None, notes=[]))
        save = mock.AsyncMock(side_effect=RuntimeError("db down") if save_error
                              else None, return_value=list(not_landed))
        service = "app.agents.adzump.services.product_service."
        with mock.patch(service + "stored_competitor_entries",
                        new=mock.AsyncMock(return_value=[dict(c) for c in saved])), \
             mock.patch(service + "save_competitors", new=save), \
             mock.patch(service + "remove_competitors", new=mock.AsyncMock()) as m_remove, \
             mock.patch(service + "reload_competitor_list", new=mock.AsyncMock()), \
             mock.patch(service + "drop_deleted_competitors", new=mock.AsyncMock(return_value=[])), \
             mock.patch("app.agents.adzump.agents.product.agent.get_product_agent",
                        return_value=analyst), \
             mock.patch.object(competitor, "_filter_self_references"), \
             mock.patch("app.core.streaming.pre_emit_agent_started", new=mock.AsyncMock()):
            result = asyncio.run(competitor._analyze_competitors(params, context))
        return result, session_ctx, save, m_remove

    def test_remove_in_a_chat_that_never_loaded_the_list(self):
        result, session_ctx, _, m_remove = self._run({"remove": "Sobha Magnus"})
        self.assertTrue(result.success)
        self.assertEqual([c["name"] for c in m_remove.await_args.args[2]], ["Sobha Magnus"])
        self.assertEqual([c["name"] for c in session_ctx["competitor_analysis"]["competitors"]],
                         ["Nambiar Villas"])

    def test_nothing_matched_leaves_the_chat_without_a_list(self):
        result, session_ctx, _, m_remove = self._run({"remove": "Ghost Towers"})
        self.assertFalse(result.success)
        self.assertNotIn("competitor_analysis", session_ctx)
        m_remove.assert_not_awaited()

    def test_add_by_name_is_the_users_pick(self):
        prestige = {"name": "Prestige Lakeside", "url": "https://prestige.com/lakeside"}
        rows = [  # (case, not landed, in skipped)
            ("landed", (), False),
            ("clashes with a saved competitor", (prestige,), True),
        ]
        for case, not_landed, skipped in rows:
            with self.subTest(case):
                result, _, save, _ = self._run(
                    {"query": "Prestige Lakeside"}, found=[dict(prestige)], not_landed=not_landed)
                self.assertTrue(save.await_args.kwargs["named_by_user"])
                self.assertEqual("clashes with a saved competitor" in result.summary, skipped)

    def test_forced_research_merges_into_the_saved_list(self):
        found = [{"name": "Nambiar Bannerghatta Villas"}, {"name": "Prestige Lakeside"}]
        result, session_ctx, save, _ = self._run({"force": "true"}, found=found)
        self.assertTrue(result.success)
        names = [c["name"] for c in session_ctx["competitor_analysis"]["competitors"]]
        self.assertEqual(names, ["Sobha Magnus", "Nambiar Villas", "Prestige Lakeside"])
        self.assertIn("1 new", result.summary)

    def test_a_failed_save_keeps_the_research(self):
        result, session_ctx, _, _ = self._run(
            {"force": "true"}, found=[{"name": "Prestige Lakeside"}], save_error=True)
        self.assertTrue(result.success)
        self.assertIn("aren't saved yet", result.model_summary)
        self.assertIn("Prestige Lakeside",
                      [c["name"] for c in session_ctx["competitor_analysis"]["competitors"]])


if __name__ == "__main__":
    unittest.main()
