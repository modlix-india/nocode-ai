"""tools/competitor.py helpers: the competitor_id -> verified URL join (B2),
user URL pins (verify + apply), and the ladder's respect for them."""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.tools import competitor
from app.agents.adzump.tools.competitor import (
    _apply_url_updates,
    _find_competitor,
    _join_verified_urls,
    _resolve_final_entry_urls,
    _verify_competitor_url,
)


class JoinVerifiedUrlsTests(unittest.TestCase):
    """The analyst cites evidence by ID; code attaches the verified URL. A
    model-written url is discarded when the ID resolves; unknown or absent
    IDs leave the entry untouched (the CP-4 ladder still runs after)."""

    SESSION = {"_research_state": {"verified_competitors": [
        {"cid": "C1", "name": "Sobha Magnus",
         "url": "https://propsoch.com/sobha-magnus",
         "fetch_url": "https://www.sobha.com/sobha-magnus/"},
        {"cid": "C2", "name": "Lodha Azur", "url": "https://lodhagroup.com/azur"},
    ]}}

    def test_join_rows(self):
        rows = [
            ("id joins fetch_url, model url discarded",
             {"name": "Sobha Magnus", "competitor_id": "C1",
              "url": "https://sobha-magnus-typo.com"},
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
        ]
        for label, comp, expected_url in rows:
            with self.subTest(label):
                competitive = {"competitors": [comp]}
                _join_verified_urls(competitive, dict(self.SESSION))
                self.assertEqual(comp.get("url"), expected_url)
                self.assertNotIn("competitor_id", comp)  # transport key stripped

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
            ("mismatch rejected with what the page is",
             "https://sobha.com/other",
             dict(answer="MATCH: NO\nA different Sobha project page."),
             "", "different Sobha project"),
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


class UserPinnedUrlLadderTests(unittest.TestCase):
    def test_ladder_never_overrides_a_user_pin(self):
        pinned = {"name": "Nambiar Villas", "url": "https://real.example",
                  "url_source": "user"}
        unpinned = {"name": "Sobha Magnus", "url": "https://propsoch.com/x"}
        with mock.patch.object(
            competitor, "resolve_project_url",
            new=mock.AsyncMock(return_value="https://ladder.example"),
        ) as ladder:
            asyncio.run(_resolve_final_entry_urls([pinned, unpinned], {}))
        self.assertEqual(pinned["url"], "https://real.example")
        self.assertEqual(unpinned["url"], "https://ladder.example")
        ladder.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
