"""competitor_urls: the GBP guards (name normalize/match), the broker-TLD
prior, project-page extraction, and the shared session memo. The CP-4 ladder
and its D-6 token test are retired - the researcher judges URLs from the
options comp_discovery gathers."""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.competitor_urls import (
    cached_business_listing,
    is_broker_style_tld,
    listing_name_matches,
    normalize_business_name,
    project_page_from_site,
)


def _session(address: str = "") -> dict:
    return {"product_data": {"place": {"lat": 12.9, "lng": 77.6,
                                       "address": address}}}


class NormalizeNameLock(unittest.TestCase):

    def test_brand_canonicalisation(self):
        cases = [
            ("Valmark CityVille", "valmark cityville"),
            ("Sumadhura Group", "sumadhura"),            # " group" suffix stripped
            ("Puravankara Pvt Ltd", "puravankara"),      # " pvt ltd" stripped
            ("Sattva-Songbird", "sattva songbird"),      # punctuation → space
            ("  Sobha  ", "sobha"),                      # strip + collapse
            # Parenthetical glosses die (CP-5 v2 step 13): developer credits
            # and duplicated brand tokens must not leak into matching/tokens.
            ("Purva Sparkling Springs (Puravankara)", "purva sparkling springs"),
            ("Nambiar Villas (Nambiar Bannerghatta Villas)", "nambiar villas"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_business_name(raw), expected)


class ListingNameMatchTests(unittest.TestCase):
    def test_match_rows(self):
        rows = [
            ("exact", "Lodha Azur", "Lodha Azur", True),
            ("listing has suffix", "Lodha Azur", "Lodha Azur by Lodha Group", True),
            ("spacing/case noise", "Purva Sparkling Springs",
             "PURVA Sparkling-Springs", True),
            ("different project, same brand", "Lodha Azur", "Lodha Bellezza", False),
            ("unrelated", "Sobha Galera", "Prestige Falcon City", False),
            ("empty listing", "Lodha Azur", "", False),
        ]
        for label, candidate, listing, expected in rows:
            with self.subTest(label):
                self.assertIs(listing_name_matches(candidate, listing), expected)


class BrokerStyleTldTests(unittest.TestCase):
    """Kailash's prior: not-plain-.com/.in hosts are broker clones ~90% of the
    time in Indian real estate - never acceptable as an entry's official URL."""

    def test_rows(self):
        rows = [
            ("nambiarvillasbannerghatta.co.in", True),   # .co.in swarm
            ("nambiarbannerghatta.info", True),
            ("prestigesouthernstar.live", True),
            ("myanimelist.net:443", True),               # port stripped first
            ("purvasparklingspring.com", False),
            ("valmark.in", False),
            ("", False),
        ]
        for host, expected in rows:
            with self.subTest(host):
                self.assertIs(is_broker_style_tld(host), expected)


class ListingMemoTests(unittest.TestCase):
    def test_one_lookup_per_name_and_misses_memoized(self):
        session = _session()
        client = mock.Mock()
        client.find_business_listings = mock.AsyncMock(return_value=[])
        with mock.patch(
            "app.agents.adzump.adapters.google.maps.GoogleMapsClient",
            return_value=client,
        ):
            for _ in range(2):  # miss memoized - no retry storm
                result = asyncio.run(
                    cached_business_listing("Sobha Magnus", session))
        self.assertIsNone(result)
        self.assertEqual(client.find_business_listings.await_count, 1)
        self.assertIn("sobha magnus", session["_places_listings_cache"])

    def test_top_listing_without_website_is_none_for_top1_view(self):
        # A lower listing's website must not masquerade as the top match.
        session = _session()
        client = mock.Mock()
        client.find_business_listings = mock.AsyncMock(return_value=[
            {"name": "Sobha Magnus", "website": ""},
            {"name": "Sobha Magnus Broker Deals",
             "website": "https://sobhamagnusdeals.com/"},
        ])
        with mock.patch(
            "app.agents.adzump.adapters.google.maps.GoogleMapsClient",
            return_value=client,
        ):
            result = asyncio.run(
                cached_business_listing("Sobha Magnus", session))
        self.assertIsNone(result)


class ProjectPageFromSiteTests(unittest.TestCase):
    """Extraction feeds the researcher's URL options: same-host guarded (a
    cross-host answer is a hallucination or an outbound link) and
    session-memoized, misses included."""

    def _extract(self, answer, session=None):
        async def fake_fetch(url, question):
            return {"status": "ok", "answer": answer, "url": url, "title": "t"}
        with mock.patch(
            "app.agents.adzump.agents.product.adapters"
            ".web_fetch_adapter.fetch_and_answer",
            new=fake_fetch,
        ) as fetch:
            return asyncio.run(project_page_from_site(
                "Sobha Magnus", "https://sobha.com/", session
                if session is not None else _session()))

    def test_rows(self):
        rows = [
            ("same-host page extracted",
             "OFFICIAL_URL: https://sobha.com/sobha-magnus",
             "https://sobha.com/sobha-magnus"),
            ("cross-host answer rejected",
             "OFFICIAL_URL: https://someotherhost.com/magnus", None),
            ("none answer", "OFFICIAL_URL: none", None),
        ]
        for label, answer, expected in rows:
            with self.subTest(label):
                self.assertEqual(self._extract(answer), expected)

    def test_memoized_per_host_and_name(self):
        session = _session()
        calls = []

        async def fake_fetch(url, question):
            calls.append(url)
            return {"status": "ok", "answer": "OFFICIAL_URL: none",
                    "url": url, "title": "t"}

        with mock.patch(
            "app.agents.adzump.agents.product.adapters"
            ".web_fetch_adapter.fetch_and_answer",
            new=fake_fetch,
        ):
            for _ in range(2):  # miss memoized too
                asyncio.run(project_page_from_site(
                    "Sobha Magnus", "https://sobha.com/", session))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
