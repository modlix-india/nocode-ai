"""comp_discovery: the extract_candidates pool, the fetch_candidates ID
enforcement, _is_specific_geography, _resolve_urls (candidate-stage URL fill)
and the dead-site fetch fallback. The GBP guards and the listing memo are
tested in test_competitor_urls.py."""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.agents.product.tools import comp_discovery
from app.agents.adzump.agents.product.tools.comp_discovery import (
    _dedupe_resolved_hosts,
    _extract_candidates,
    _fetch_candidates,
    _fetch_one_candidate,
    _is_specific_geography,
    _merge_candidate_facts,
    _resolve_urls,
)


def _context(search_results=None) -> dict:
    return {"session_context": {
        "product_profile": {"url": "https://cityville.in",
                            "summary": "Luxury villaments on Bannerghatta Road"},
        "product_data": {"product_name": "Valmark CityVille",
                         "place": {"lat": 12.9, "lng": 77.6}},
        "_research_state": {"search_results": search_results or []},
    }}


def _searches() -> list[dict]:
    return [
        {"query": "q1", "candidates": [
            {"name": "Purva Sparkling Springs",
             "url": "https://purvasparklingspring.com/"},
            {"name": "Valmark CityVille", "url": "https://cityville.in/"},
        ]},
        {"query": "q2", "candidates": [
            {"name": "Purva Sparkling Springs",
             "url": "https://purvasparklingspring.com/"},
            {"name": "Lodha Azur", "url": "https://99acres.com/lodha-azur"},
        ]},
    ]


class IsSpecificGeographyLock(unittest.TestCase):

    def test_rows(self):
        # A marker word (road/layout/block) or a compound suffix (-nagar) is a
        # micro-market; a city, region or nothing is not.
        specific = ["Sarjapur Road", "HSR Layout", "Indiranagar",
                    "Koramangala 5th Block", "Whitefield Main Road"]
        broad = ["Bengaluru", "Karnataka", "India", "", None]
        for geo, expected in [(g, True) for g in specific] + [(g, False) for g in broad]:
            with self.subTest(geo=geo):
                self.assertEqual(_is_specific_geography(geo), expected)


class SelfReferenceBrandHostTests(unittest.TestCase):
    """The client's own developer domain must never enter the competitor list,
    even under an SEO title the name match can't catch (live 2026-09-04:
    valmark.in surfaced for a Valmark CityVille campaign). Leading brand token
    only - a locality word in the product name must not condemn strangers."""

    def _facts(self, primary_name, candidate_name, candidate_url):
        results = [{"query": "q1", "candidates": [
            {"name": candidate_name, "url": candidate_url}]}]
        return _merge_candidate_facts(results, primary_host="cityville.in",
                                      primary_name=primary_name)[0]

    def test_rows(self):
        rows = [
            ("developer root under SEO title", "Valmark CityVille",
             "3 & 4 BHK Villaments in Bannerghatta Road",
             "https://valmark.in/cityville", True),
            ("locality token never condemns strangers", "Godrej Bannerghatta",
             "Nambiar Villas", "https://nambiarbannerghattaroad.co.in/", False),
            ("unrelated brand host stays", "Valmark CityVille",
             "Sobha Magnus", "https://sobha.com/sobha-magnus", False),
        ]
        for label, primary, cand_name, cand_url, expected in rows:
            with self.subTest(label):
                self.assertIs(self._facts(primary, cand_name, cand_url)
                              ["is_primary"], expected)


class ExtractCandidatesTests(unittest.TestCase):
    """The tool returns facts only - IDs, hosts, frequency, flags - and holds
    URL custody in the session pool. Judgment is the agent's."""

    def _run(self, search_results):
        context = _context(search_results)
        result = asyncio.run(_extract_candidates({}, context))
        return result, context["session_context"]["_research_state"]

    def test_pool_facts_and_url_custody(self):
        result, research_state = self._run(_searches())
        self.assertTrue(result.success)
        pool = research_state["candidate_pool"]
        self.assertEqual(set(pool), {"C1", "C2"})
        self.assertEqual(len(pool["C1"]["seen_in"]), 2)
        self.assertTrue(pool["C2"]["is_aggregator"])  # Lodha on 99acres
        # URL custody stays in the pool; the model never sees a full URL.
        self.assertEqual(pool["C1"]["url"], "https://purvasparklingspring.com/")
        self.assertNotIn("https://", result.model_summary)
        # The user's row is a plain line; the table is the model's.
        self.assertEqual(result.summary, "2 possible competitors from 2 searches")

    def test_piped_title_cannot_shift_table_columns(self):
        result, _ = self._run([{"query": "q1", "candidates": [
            {"name": "Sobha Magnus | Luxury Flats\nBannerghatta",
             "url": "https://sobha.com/magnus"}]}])
        row = next(line for line in result.model_summary.splitlines()
                   if line.startswith("C1 |"))
        self.assertEqual(len(row.split(" | ")), 5)  # ID | name | host | seen in | flags

    def test_pool_rebuild_drops_mismatched_verified_evidence(self):
        # Run boundary: cids are positional per pool build. A later run's C1
        # must never inherit an earlier run's C1 evidence (wrong URL under a
        # wrong name); same-name same-cid evidence survives (bounce re-runs).
        context = _context(_searches())
        asyncio.run(_extract_candidates({}, context))
        research_state = context["session_context"]["_research_state"]
        research_state["verified_competitors"] = [
            {"cid": "C1", "name": "Purva Sparkling Springs",
             "fetch_url": "https://purvasparklingspring.com/"},  # still C1
            {"cid": "C2", "name": "Some Other Business",
             "fetch_url": "https://other.example/"},             # stale
        ]
        asyncio.run(_extract_candidates({}, context))
        kept = research_state["verified_competitors"]
        self.assertEqual([v["name"] for v in kept],
                         ["Purva Sparkling Springs"])


class FetchCandidatesTests(unittest.TestCase):
    """ID enforcement: unknown IDs and over-budget picks are evidence-bearing
    errors; verified evidence accumulates across calls (already-verified IDs
    skip re-fetch); the evidence block carries no SEGMENT hints."""

    def _run(self, ids, context, fetch_status="ok"):
        self.fetched = []

        async def fake_fetch(candidate):
            self.fetched.append(candidate["cid"])
            return {**candidate, "fetch_status": fetch_status,
                    "fetch_answer": "TYPE: BRAND\nGood page",
                    "fetch_url": candidate.get("url")}
        with mock.patch.object(comp_discovery, "_resolve_urls",
                               new=mock.AsyncMock()), \
             mock.patch.object(comp_discovery, "_fetch_one_candidate",
                               new=fake_fetch):
            return asyncio.run(_fetch_candidates({"ids": ids}, context))

    def _prepared_context(self):
        context = _context(_searches())
        asyncio.run(_extract_candidates({}, context))
        return context

    def _verified_cids(self, context):
        return [c["cid"] for c in
                context["session_context"]["_research_state"]["verified_competitors"]]

    def test_verifies_picked_ids_and_lists_them_as_citable(self):
        context = self._prepared_context()
        result = self._run(["C1"], context)
        self.assertTrue(result.success)
        self.assertEqual(self._verified_cids(context), ["C1"])
        self.assertIn("C1 = Purva Sparkling Springs", result.model_summary)  # citable roster
        self.assertEqual(result.summary, "1 verified: Purva Sparkling Springs")

    def test_bad_calls_error_before_any_fetch(self):
        over_budget = _context([{"query": "q1", "candidates": [
            {"name": f"Project {i}", "url": f"https://project{i}.com/"}
            for i in range(14)]}])
        asyncio.run(_extract_candidates({}, over_budget))
        rows = [
            ("no search results", lambda: asyncio.run(
                _extract_candidates({}, _context([]))), "web_search first"),
            ("unknown id", lambda: self._run(["C1", "C9"], self._prepared_context()),
             "Unknown candidate IDs: C9"),
            ("no ids", lambda: self._run([], self._prepared_context()),
             "Pass the candidate IDs"),
            ("over budget", lambda: self._run(
                [f"C{i}" for i in range(1, 15)], over_budget), "over the fetch budget"),
            ("no pool", lambda: self._run(["C1"], _context(_searches())),
             "extract_candidates first"),
        ]
        for label, call, error_part in rows:
            with self.subTest(label):
                self.fetched = []
                result = call()
                self.assertFalse(result.success)
                self.assertIn(error_part, result.error)
                self.assertEqual(self.fetched, [])

    def test_second_call_accumulates_and_skips_verified(self):
        context = self._prepared_context()
        self._run(["C1"], context)
        self._run(["C1", "C2"], context)
        self.assertEqual(self.fetched, ["C2"])  # C1 is never re-fetched
        self.assertEqual(set(self._verified_cids(context)), {"C1", "C2"})

    def test_calls_that_verify_nothing_keep_the_evidence_base(self):
        with self.subTest("first-call washout stays recoverable"):
            context = self._prepared_context()
            result = self._run(["C1"], context, fetch_status="failed")
            self.assertTrue(result.success)
            self.assertEqual(self._verified_cids(context), [])
        context = self._prepared_context()
        self._run(["C1"], context)
        for label, ids, status in [
            ("all cited IDs already verified", ["C1"], "ok"),
            ("replacement picks all failed", ["C2"], "failed"),
        ]:
            with self.subTest(label):
                result = self._run(ids, context, fetch_status=status)
                self.assertTrue(result.success)
                self.assertEqual(self._verified_cids(context), ["C1"])
                self.assertIn("C1 = Purva Sparkling Springs", result.model_summary)
                self.assertTrue(result.summary.startswith("Nothing new - keeping the 1"))


class FullJudgmentInputTests(unittest.TestCase):
    """The analyst reads its candidate table and fetch evidence whole (live
    2026-09-29: both were cut at 4000 chars, it never saw the later rows or
    the citable-ID roster, and kept 1-2 competitors)."""

    def test_a_large_table_and_evidence_reach_the_model_whole(self):
        searches = [{"query": f"q{q}", "candidates": [
            {"name": f"Project {q}-{i} Luxury Villas for Sale in Bannerghatta Road "
                     f"| 3 BHK, 4 BHK Villas with Lake View",
             "url": f"https://project{q}x{i}.com/"} for i in range(9)]}
            for q in range(7)]
        context = _context(searches)
        table = asyncio.run(_extract_candidates({}, context))
        sent = table.to_tool_result_content()
        self.assertGreater(len(table.model_summary), 4000)
        self.assertNotIn("[truncated", sent)
        self.assertIn("then call fetch_candidates", sent)  # the closing instructions

        async def fake_fetch(candidate):
            return {**candidate, "fetch_status": "ok",
                    "fetch_answer": "TYPE: BRAND\n" + "Verified project facts. " * 90,
                    "fetch_url": candidate.get("url")}

        async def fake_options(candidate, session_ctx):
            candidate["url_options"] = {f"{candidate['cid']}.U1": candidate["url"]}
            candidate["url_option_lines"] = [f"{candidate['cid']}.U1 | reads as: " + "x" * 280]

        ids = [f"C{i}" for i in range(1, 9)]
        with mock.patch.object(comp_discovery, "_resolve_urls", new=mock.AsyncMock()), \
             mock.patch.object(comp_discovery, "_fetch_one_candidate", new=fake_fetch), \
             mock.patch.object(comp_discovery, "_attach_url_options", new=fake_options):
            evidence = asyncio.run(_fetch_candidates({"ids": ids}, context))
        sent = evidence.to_tool_result_content()
        self.assertGreater(len(evidence.model_summary), 4000)
        self.assertNotIn("[truncated", sent)
        self.assertIn("Citable IDs", sent)
        self.assertIn("C8 = ", sent)


class FetchRowTests(unittest.TestCase):
    """The user's tool row names what verified by its title's lead phrase
    (live 2026-09-29: rows showed the model's ID table)."""

    def test_short_name_rows(self):
        for title, expected in [
            ("Rainbow Mayfair Begur - Brochure, Pros&Cons", "Rainbow Mayfair Begur"),
            ("Godrej Vanantara Bannerghatta Road, Bangalore | New", "Godrej Vanantara Bannerghatta Road"),
            ("Introducing SOBHA Magnus: Biophilic living", "Introducing SOBHA Magnus"),
            ("Nambiar District-25 Phase 2", "Nambiar District-25 Phase 2"),
            ("", "sobha.com"),
        ]:
            with self.subTest(title):
                self.assertEqual(
                    comp_discovery._short_name({"name": title, "host": "sobha.com"}), expected)


class ThinkingIdNamesTests(unittest.TestCase):
    """The analyst's thinking reaches the user with each candidate ID named
    (live 2026-09-29: "I can't confirm C18, C15, C12, or C1")."""

    STATE = {"candidate_pool": {
        "C6": {"name": "Rainbow Mayfair | Luxury Villas", "host": "rainbowmayfair.com",
               "url_options": {"C6.U2": "https://www.rainbowmayfair.com/villas/"}},
        "C16": {"name": "Valmark Cityville - Villas", "host": "valmarkcityville.com"},
    }}

    def test_rows(self):
        for label, text, expected in [
            ("a bare list names each",
             "picking C6 and C16",
             "picking Rainbow Mayfair (rainbowmayfair.com) and Valmark Cityville (valmarkcityville.com)"),
            ("an ID the analyst named stays",
             "C6 Rainbow Mayfair (rainbowmayfair.com) PICK",
             "C6 Rainbow Mayfair (rainbowmayfair.com) PICK"),
            ("a URL option becomes its short link",
             "C6.U2 is the project page",
             "rainbowmayfair.com/villas is the project page"),
            ("an unknown ID stays", "C99 skipped", "C99 skipped"),
            ("a word starting with C is not an ID", "Cityville C-grade", "Cityville C-grade"),
        ]:
            with self.subTest(label):
                self.assertEqual(comp_discovery.name_candidate_ids(text, self.STATE), expected)

    def test_split_partial_id_rows(self):
        for text, expected in [
            ("picking C1", ("picking ", "C1")),
            ("picking C6.U", ("picking ", "C6.U")),
            ("picking C", ("picking ", "C")),
            ("picking C6 now", ("picking C6 now", "")),
            ("ABC", ("ABC", "")),
        ]:
            with self.subTest(text):
                self.assertEqual(comp_discovery.split_partial_id(text), expected)


class AttachUrlOptionsTests(unittest.TestCase):
    """The researcher owns URL judgment: code gathers per-candidate vetted
    options (read page + GBP top-3 + extraction), aggregator/broker/dead hosts
    never become citable, and the uid->url custody map feeds the join."""

    def _attach(self, candidate, listings, extracted=None, alive=True):
        from app.agents.adzump.agents.product.tools.comp_discovery import (
            _attach_url_options,
        )
        with mock.patch.object(
            comp_discovery, "cached_business_listings",
            new=mock.AsyncMock(return_value=listings),
        ), mock.patch.object(
            comp_discovery, "project_page_from_site",
            new=mock.AsyncMock(return_value=extracted),
        ), mock.patch.object(
            comp_discovery, "is_alive",
            new=mock.AsyncMock(return_value=alive),
        ), mock.patch.object(
            comp_discovery, "_read_page_line",
            new=mock.AsyncMock(return_value="official site of the project"),
        ):
            asyncio.run(_attach_url_options(candidate, {}))
        return candidate

    def test_options_gathered_vetted_and_id_mapped(self):
        candidate = self._attach(
            {"cid": "C3", "name": "Sobha Magnus",
             "fetch_url": "https://propsoch.com/sobha-magnus"},
            listings=[
                {"name": "SOBHA Magnus", "website": "https://www.sobha.com/"},
                {"name": "Broker Deals",
                 "website": "https://sobhamagnus.co.in/"},   # broker TLD
                {"name": "Maps", "website": "https://google.com/maps/x"},
            ],
            extracted="https://www.sobha.com/sobha-magnus/")
        options = candidate["url_options"]
        self.assertEqual(list(options), ["C3.U1", "C3.U2", "C3.U3"])
        self.assertEqual(options["C3.U1"], "https://propsoch.com/sobha-magnus")
        self.assertEqual(options["C3.U2"], "https://www.sobha.com/")
        self.assertEqual(options["C3.U3"], "https://www.sobha.com/sobha-magnus/")
        notes = " ".join(candidate["url_option_notes"])
        self.assertIn("broker-style", notes)
        self.assertIn("aggregator", notes)
        lines = " ".join(candidate["url_option_lines"])
        self.assertIn('listing "SOBHA Magnus"', lines)
        self.assertIn("reads as: official site of the project", lines)

    def test_dead_options_are_never_citable(self):
        candidate = self._attach(
            {"cid": "C1", "name": "Rainbow Mayfair",
             "fetch_url": "https://rainbowmayfaire.com/"},
            listings=[{"name": "Rainbow Mayfair",
                       "website": "https://rainbowmayfair.com/"}],
            alive=False)  # the GBP option is dead
        self.assertEqual(list(candidate["url_options"]), ["C1.U1"])
        self.assertIn("dead", " ".join(candidate["url_option_notes"]))

    def test_duplicate_urls_merge_into_one_option(self):
        candidate = self._attach(
            {"cid": "C2", "name": "Purva Sparkling Springs",
             "fetch_url": "https://purvasparklingspring.com/"},
            listings=[{"name": "Purva Sparkling Springs",
                       "website": "https://purvasparklingspring.com"}])
        self.assertEqual(len(candidate["url_options"]), 1)
        self.assertIn('listing "Purva Sparkling Springs"',
                      " ".join(candidate["url_option_lines"]))


class ResolveUrlsTests(unittest.TestCase):
    """Candidate-stage URL fill (D-5, scoped back by CP-4): only MISSING or
    aggregator URLs spend a GBP lookup (junk SEO titles rarely pass the name
    guard; the final-entry ladder revisits with clean names); a guard-passing
    listing WINS, a wrong or shared-host listing never lands (a bad URL would
    poison the shared creative-library key), a guard miss keeps the URL."""

    def _run(self, candidates, listing):
        session = {"product_data": {"place": {"lat": 12.9, "lng": 77.6}}}
        client = mock.Mock()
        listings = listing if isinstance(listing, list) else [listing]
        client.find_business_listings = mock.AsyncMock(
            side_effect=[[item] if item else [] for item in listings])
        with mock.patch(
            "app.agents.adzump.adapters.google.maps.GoogleMapsClient",
            return_value=client,
        ):
            asyncio.run(_resolve_urls(candidates, session))
        return client

    def test_gbp_fills_missing_and_displaces_aggregator_urls(self):
        candidates = [
            {"name": "Purva Sparkling Springs", "url": "https://99acres.com/x"},
            {"name": "Lodha Azur", "url": None},
        ]
        client = self._run(candidates, [
            {"name": "Purva Sparkling Springs",
             "website": "https://purvasparklingspring.com/"},
            {"name": "Lodha Azur", "website": "https://lodhagroup.com/azur"},
        ])
        self.assertEqual(candidates[0]["url"], "https://purvasparklingspring.com/")
        self.assertEqual(candidates[0]["search_url"], "https://99acres.com/x")
        self.assertEqual(candidates[1]["url"], "https://lodhagroup.com/azur")
        self.assertNotIn("search_url", candidates[1])  # nothing displaced
        self.assertEqual(client.find_business_listings.await_count, 2)
        kwargs = client.find_business_listings.await_args.kwargs
        self.assertEqual((kwargs["lat"], kwargs["lng"]), (12.9, 77.6))

    def test_good_search_url_spends_no_lookup(self):
        candidates = [
            {"name": "Purva Sparkling Springs",
             "url": "https://www.puravankara.com/villas-in-bannerghatta-road"},
        ]
        client = self._run(candidates, [])
        self.assertEqual(candidates[0]["url"],
                         "https://www.puravankara.com/villas-in-bannerghatta-road")
        self.assertEqual(client.find_business_listings.await_count, 0)

    def test_guard_misses_keep_the_search_url(self):
        rows = [
            ("name mismatch", {"name": "Lodha Bellezza",
                               "website": "https://lodhagroup.com/bellezza"}),
            ("shared host", {"name": "Lodha Azur",
                             "website": "https://facebook.com/lodhaazur"}),
            ("no listing", None),
        ]
        for label, listing in rows:
            with self.subTest(label):
                candidates = [{"name": "Lodha Azur", "url": "https://99acres.com/x"}]
                self._run(candidates, listing)
                self.assertEqual(candidates[0]["url"], "https://99acres.com/x")


class DedupeResolvedHostsTests(unittest.TestCase):
    def test_keeps_first_per_host_and_urlless_pass_through(self):
        candidates = [
            {"name": "Purva A", "url": "https://puravankara.com/a"},
            {"name": "Purva B", "url": "https://www.puravankara.com/b"},
            {"name": "No Site", "url": None},
        ]
        kept = _dedupe_resolved_hosts(candidates)
        self.assertEqual([c["name"] for c in kept], ["Purva A", "No Site"])


class FetchFallbackTests(unittest.TestCase):
    """A dead GBP website retries once with the displaced search URL (D-5)."""

    def _fetch(self, candidate, answers_by_url):
        async def fake_fetch(url, question):
            answer = answers_by_url.get(url)
            if answer is None:
                raise ConnectionError("dead site")
            return {"status": "ok", "answer": answer, "url": url, "title": "t"}
        with mock.patch(
            "app.agents.adzump.agents.product.adapters.web_fetch_adapter.fetch_and_answer",
            new=fake_fetch,
        ):
            return asyncio.run(_fetch_one_candidate(candidate))

    def test_dead_site_fallback(self):
        live = {"https://puravankara.com/villas": "TYPE: BRAND\nGood page"}
        for label, search_url, status, url in [
            ("dead GBP url falls back to the search url",
             "https://puravankara.com/villas", "ok", "https://puravankara.com/villas"),
            ("both dead fails", "https://alsodead.com", "failed", None),
        ]:
            with self.subTest(label):
                result = self._fetch({"name": "Purva", "url": "https://deadmicrosite.com",
                                      "search_url": search_url}, live)
                self.assertEqual(result["fetch_status"], status)
                if url:
                    self.assertEqual(result["url"], url)


if __name__ == "__main__":
    unittest.main()
