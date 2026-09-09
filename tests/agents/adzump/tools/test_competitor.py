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
    _find_competitor,
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
             "https://nambiarprojects.com/nambiar-bannerghatta-road/", None),
            # The USER is the authority: a live non-portal pin is accepted even
            # when the page read disagrees, with the doubt as a caution (live
            # 2026-09-08: the reader vetoed the user's CORRECT Nambiar pin
            # because the official page mentions a channel partner).
            ("content mismatch accepted with caution",
             "https://sobha.com/other",
             dict(answer="MATCH: NO\nA different Sobha project page."),
             "https://sobha.com/other", "different Sobha project"),
            ("dead site rejected", "https://deadclone.co.in/",
             dict(), "", "didn't respond"),
            ("portal rejected without a fetch", "https://99acres.com/x",
             dict(answer="MATCH: YES"), "", "portal"),
            ("redirect into a portal rejected", "https://short.link/x",
             dict(answer="MATCH: YES", final_url="https://99acres.com/y"),
             "", "redirects to a portal"),
            ("garbage url rejected", "not-a-url", dict(answer="MATCH: YES"),
             "", "not a valid website"),
        ]
        for label, url, kwargs, expected_url, reason_part in rows:
            with self.subTest(label):
                verified, reason = self._verify(
                    "Nambiar Villas Bannerghatta", url,
                    answer=kwargs.get("answer"), final_url=kwargs.get("final_url"))
                self.assertEqual(verified, expected_url)
                if reason_part:
                    self.assertIn(reason_part, reason)


class ApplyUrlUpdatesTests(unittest.TestCase):
    def _apply(self, set_url, competitors, verified=("https://ok.example/", "")):
        competitive = {"competitors": competitors}
        with mock.patch.object(
            competitor, "_verify_competitor_url",
            new=mock.AsyncMock(return_value=verified),
        ):
            acks, rejections = asyncio.run(
                _apply_url_updates(set_url, competitive))
        return acks, rejections

    def test_pin_updates_entry_and_resets_creatives(self):
        entry = {"name": "Nambiar Villas", "url": "https://clone.co.in",
                 "creatives": [{"creativeId": "a"}], "totalCreatives": 1,
                 "activeCreatives": 1}
        acks, rejections = self._apply("Nambiar | https://real.example", [entry])
        self.assertEqual(len(acks), 1)
        self.assertEqual(rejections, [])
        self.assertEqual(entry["url"], "https://ok.example/")
        self.assertEqual(entry["url_source"], "user")
        for stale in ("creatives", "totalCreatives", "activeCreatives"):
            self.assertNotIn(stale, entry)

    def test_rejections_are_user_facing(self):
        rows = [
            ("verification failed",
             "Nambiar | https://wrong.example",
             [{"name": "Nambiar Villas", "url": None}],
             ("", "that page is a broker site"), "broker site"),
            ("unknown competitor", "Ghost | https://x.example",
             [{"name": "Nambiar Villas"}], ("https://x.example", ""),
             "No competitor named 'Ghost'"),
            ("unparseable spec", "just some text",
             [{"name": "Nambiar Villas"}], ("https://x.example", ""),
             "expected 'Name | URL'"),
        ]
        for label, spec, competitors, verified, reason_part in rows:
            with self.subTest(label):
                acks, rejections = self._apply(spec, competitors, verified)
                self.assertEqual(acks, [])
                self.assertIn(reason_part, rejections[0])
                self.assertNotEqual(competitors[0].get("url_source"), "user")

    def test_find_competitor_is_fuzzy(self):
        competitive = {"competitors": [
            {"name": "Purva Sparkling Springs"}, {"name": "Sobha Magnus"}]}
        self.assertEqual(_find_competitor(competitive, "purva")["name"],
                         "Purva Sparkling Springs")
        self.assertIsNone(_find_competitor(competitive, "Lodha"))


class SameProjectTests(unittest.TestCase):
    """Live 2026-09-08: 'check Nambiar's official website' appended a second
    Nambiar card. A looked-up name matching an existing entry must refresh it;
    sibling projects (same brand, different project) must stay separate."""

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
                from app.agents.adzump.tools.competitor import _same_project
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

    def test_host_change_resets_creatives_same_host_keeps(self):
        from app.agents.adzump.tools.competitor import _refresh_entry
        changed = {"name": "N", "url": "https://old.example/",
                   "creatives": [{"creativeId": "a"}], "totalCreatives": 1,
                   "activeCreatives": 1}
        _refresh_entry(changed, {"name": "N", "url": "https://new.example/"})
        self.assertNotIn("creatives", changed)
        same_host = {"name": "N", "url": "https://site.example/",
                     "creatives": [{"creativeId": "a"}]}
        _refresh_entry(same_host, {"name": "N", "url": "https://site.example/page"})
        self.assertIn("creatives", same_host)
        self.assertEqual(same_host["url"], "https://site.example/page")


class AnalyzeReentrancyAndMergeTests(unittest.TestCase):
    """Live 2026-09-08: 'find more competitors' fired several analyze calls
    (two in parallel), each force-run wiped the list and painted another
    'Competitors' panel group. One analyst at a time; re-discovery merges."""

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
        self.assertIn("ALREADY running", second.error)
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
        ):
            result = asyncio.run(
                _analyze_competitors({"force": "true"}, context))
        self.assertTrue(result.success)
        names = [c["name"] for c in
                 context["session_context"]["competitor_analysis"]["competitors"]]
        self.assertEqual(names, ["Nambiar Villas", "Sobha Magnus"])
        self.assertEqual(pinned["url"], "https://pinned.example")  # pin survived
        self.assertEqual(pinned["creatives"], [{"creativeId": "a"}])
        self.assertIn("1 new", result.summary)
        self.assertIn("1 already known", result.summary)


if __name__ == "__main__":
    unittest.main()
