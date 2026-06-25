"""Lock #4 — scrape guards (scrape/tool.py): `_is_same_website` + the 5-scrape cap.

`_is_same_website` carries a documented near-bug: it must use `removeprefix("www.")`,
NOT `lstrip("www.")` — lstrip treats "www." as a char SET {w,.} and would mangle a
real domain like "wisco.com" → "isco.com". The cap rejects re-scrapes + over-budget
calls (MAX_SCRAPE_CALLS=5).

Run:
    cd nocode-ai && ./venv/bin/python -m unittest \\
        tests.agents.adzump.test_scrape_guards -v
"""

from __future__ import annotations

import unittest

from app.agents.adzump.agents.product.tools.scrape.tool import (
    _is_same_website, _reject_if_duplicate_or_over_cap, MAX_SCRAPE_CALLS,
)


class IsSameWebsiteLock(unittest.TestCase):

    def test_same_or_subdomain_is_true(self):
        for a, b in [
            ("https://purvasparklingspring.com/", "https://purvasparklingspring.com/contact"),
            ("https://www.purvasparklingspring.com", "https://purvasparklingspring.com"),  # www stripped
            ("https://blog.purvasparklingspring.com", "https://purvasparklingspring.com"),  # subdomain
        ]:
            with self.subTest(a=a, b=b):
                self.assertTrue(_is_same_website(a, b))

    def test_different_sites_false(self):
        for a, b in [
            ("https://purvasparklingspring.com", "https://sobha.com"),
            ("https://purvasparklingspring.com", ""),
        ]:
            with self.subTest(a=a, b=b):
                self.assertFalse(_is_same_website(a, b))

    def test_wisco_mangle_guard(self):
        # The bug: lstrip("www.") would turn "wisco.com" into "isco.com",
        # making these two compare EQUAL (True). Correct (removeprefix) → False.
        self.assertFalse(_is_same_website("https://wisco.com", "https://isco.com"))


class ScrapeCapLock(unittest.TestCase):

    def test_fresh_under_cap_proceeds(self):
        r = _reject_if_duplicate_or_over_cap(
            "https://earthenambience.in", ["https://purvasparklingspring.com"], 1)
        self.assertIsNone(r)   # None = proceed

    def test_duplicate_rejected(self):
        r = _reject_if_duplicate_or_over_cap(
            "https://purvasparklingspring.com", ["https://purvasparklingspring.com"], 1)
        self.assertIsNotNone(r)
        self.assertFalse(r.success)

    def test_over_cap_rejected(self):
        r = _reject_if_duplicate_or_over_cap(
            "https://brand-new.in", ["a", "b", "c", "d", "e"], MAX_SCRAPE_CALLS)
        self.assertIsNotNone(r)
        self.assertFalse(r.success)


if __name__ == "__main__":
    unittest.main()
