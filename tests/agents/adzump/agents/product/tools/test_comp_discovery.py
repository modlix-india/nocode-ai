"""comp_discovery: the extract_candidates fact table, the fetch_candidates ID
enforcement, _is_specific_geography (the geography FLAG), and _resolve_urls
(candidate-stage URL fill, D-5 scoped by CP-4) + the dead-GBP fetch fallback.
The GBP guards' own tests live in test_competitor_urls.py."""
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

    def test_specific_localities_true(self):
        # Marker word (road/layout/block) OR compound suffix (-nagar).
        for geo in ["Sarjapur Road", "HSR Layout", "Indiranagar",
                    "Koramangala 5th Block", "Whitefield Main Road"]:
            with self.subTest(geo=geo):
                self.assertTrue(_is_specific_geography(geo))

    def test_city_regional_or_empty_false(self):
        for geo in ["Bengaluru", "Karnataka", "India", "", None]:
            with self.subTest(geo=geo):
                self.assertFalse(_is_specific_geography(geo))


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

    def test_table_facts_ids_and_pool_custody(self):
        result, research_state = self._run(_searches())
        self.assertTrue(result.success)
        pool = research_state["candidate_pool"]
        self.assertEqual(set(pool), {"C1", "C2"})
        # Facts in the table: seen-in count, aggregator flag; NO full URLs.
        self.assertIn("C1 | Purva Sparkling Springs | purvasparklingspring.com "
                      "| 2/2 | -", result.summary)
        self.assertIn("aggregator-hosted", result.summary)  # Lodha on 99acres
        self.assertNotIn("https://", result.summary)
        # URL custody stays in the pool for fetch_candidates.
        self.assertEqual(pool["C1"]["url"], "https://purvasparklingspring.com/")

    def test_self_reference_excluded_and_surfaced(self):
        result, research_state = self._run(_searches())
        self.assertNotIn("Valmark CityVille |", result.summary)
        self.assertIn("Excluded as the client's own business: Valmark CityVille",
                      result.summary)
        self.assertNotIn("Valmark CityVille",
                         [c["name"] for c in
                          research_state["candidate_pool"].values()])

    def test_geography_flag_for_micro_market_profiles(self):
        result, _ = self._run(_searches())  # profile says "Bannerghatta Road"
        self.assertIn("Geography flag", result.summary)

    def test_piped_title_cannot_shift_table_columns(self):
        result, _ = self._run([{"query": "q1", "candidates": [
            {"name": "Sobha Magnus | Luxury Flats\nBannerghatta",
             "url": "https://sobha.com/magnus"}]}])
        self.assertIn("C1 | Sobha Magnus / Luxury Flats Bannerghatta "
                      "| sobha.com | 1/1 | -", result.summary)

    def test_no_search_results_errors(self):
        result, _ = self._run([])
        self.assertFalse(result.success)
        self.assertIn("web_search first", result.error)


class FetchCandidatesTests(unittest.TestCase):
    """ID enforcement: unknown IDs and over-budget picks are evidence-bearing
    errors; verified evidence accumulates across calls (already-verified IDs
    skip re-fetch); the evidence block carries no SEGMENT hints."""

    def _run(self, ids, context, fetch_status="ok"):
        async def fake_fetch(candidate):
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

    def test_verifies_picked_ids_and_stashes_evidence(self):
        context = self._prepared_context()
        result = self._run(["C1"], context)
        self.assertTrue(result.success)
        self.assertIn("### Purva Sparkling Springs", result.summary)
        self.assertIn("ID: C1", result.summary)
        self.assertNotIn("SEGMENT", result.summary)
        verified = context["session_context"]["_research_state"]["verified_competitors"]
        self.assertEqual([c["cid"] for c in verified], ["C1"])

    def test_id_enforcement_rows(self):
        context = self._prepared_context()
        rows = [
            ("unknown id", ["C1", "C9"], "Unknown candidate IDs: C9"),
            ("no ids", [], "Pass the candidate IDs"),
        ]
        for label, ids, error_part in rows:
            with self.subTest(label):
                result = self._run(ids, context)
                self.assertFalse(result.success)
                self.assertIn(error_part, result.error)

    def test_over_budget_picks_error(self):
        context = _context([{"query": "q1", "candidates": [
            {"name": f"Project {i}", "url": f"https://project{i}.com/"}
            for i in range(14)]}])
        asyncio.run(_extract_candidates({}, context))
        result = self._run([f"C{i}" for i in range(1, 15)], context)
        self.assertFalse(result.success)
        self.assertIn("over the fetch budget", result.error)

    def test_no_pool_errors(self):
        result = self._run(["C1"], _context(_searches()))
        self.assertFalse(result.success)
        self.assertIn("extract_candidates first", result.error)

    def test_second_call_accumulates_and_skips_verified(self):
        context = self._prepared_context()
        self._run(["C1"], context)
        result = self._run(["C1", "C2"], context)
        self.assertIn("Already verified earlier, not re-fetched: C1",
                      result.summary)
        verified = context["session_context"]["_research_state"]["verified_competitors"]
        self.assertEqual({c["cid"] for c in verified}, {"C1", "C2"})

    def test_nothing_verified_is_honest_and_recoverable(self):
        context = self._prepared_context()
        result = self._run(["C1"], context, fetch_status="failed")
        self.assertTrue(result.success)
        self.assertIn("NOTHING VERIFIED", result.summary)
        self.assertIn("ONCE more with different IDs", result.summary)


class ResolveUrlsTests(unittest.TestCase):
    """Candidate-stage URL fill (D-5, scoped back by CP-4): only MISSING or
    aggregator URLs spend a GBP lookup (junk SEO titles rarely pass the name
    guard; the final-entry ladder revisits with clean names); a guard-passing
    listing WINS, a wrong or shared-host listing never lands (a bad URL would
    poison the shared creative-library key), a guard miss keeps the URL."""

    def _run(self, candidates, listing, session=None):
        session = session or {"product_data": {"place": {"lat": 12.9, "lng": 77.6}}}
        client = mock.Mock()
        listings = listing if isinstance(listing, list) else [listing]
        client.find_business_website = mock.AsyncMock(side_effect=listings)
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
        self.assertEqual(client.find_business_website.await_count, 2)
        kwargs = client.find_business_website.await_args.kwargs
        self.assertEqual((kwargs["lat"], kwargs["lng"]), (12.9, 77.6))

    def test_good_search_url_spends_no_lookup(self):
        candidates = [
            {"name": "Purva Sparkling Springs",
             "url": "https://www.puravankara.com/villas-in-bannerghatta-road"},
        ]
        client = self._run(candidates, [])
        self.assertEqual(candidates[0]["url"],
                         "https://www.puravankara.com/villas-in-bannerghatta-road")
        self.assertEqual(client.find_business_website.await_count, 0)

    def test_memo_spends_one_lookup_per_name(self):
        session = {"product_data": {"place": {"lat": 12.9, "lng": 77.6}}}
        listing = {"name": "Lodha Azur", "website": "https://lodhagroup.com/azur"}
        first = [{"name": "Lodha Azur", "url": None}]
        client = self._run(first, [listing], session=session)
        self.assertEqual(client.find_business_website.await_count, 1)
        repeat = [{"name": "Lodha Azur", "url": None}]
        client = self._run(repeat, [], session=session)  # memo hit - no call
        self.assertEqual(client.find_business_website.await_count, 0)
        self.assertEqual(repeat[0]["url"], "https://lodhagroup.com/azur")

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

    def test_dead_gbp_url_falls_back_to_search_url(self):
        result = self._fetch(
            {"name": "Purva", "url": "https://deadmicrosite.com",
             "search_url": "https://puravankara.com/villas"},
            {"https://puravankara.com/villas": "TYPE: BRAND\nGood page"},
        )
        self.assertEqual(result["fetch_status"], "ok")
        self.assertEqual(result["url"], "https://puravankara.com/villas")

    def test_both_dead_fails(self):
        result = self._fetch(
            {"name": "Purva", "url": "https://deadmicrosite.com",
             "search_url": "https://alsodead.com"},
            {},
        )
        self.assertEqual(result["fetch_status"], "failed")


if __name__ == "__main__":
    unittest.main()
