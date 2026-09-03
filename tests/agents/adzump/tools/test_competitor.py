"""tools/competitor.py helpers: the competitor_id -> verified URL join (B2)."""
from __future__ import annotations

import unittest

from app.agents.adzump.tools.competitor import _join_verified_urls


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


if __name__ == "__main__":
    unittest.main()
