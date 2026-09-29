"""Lock #4 - scrape guards (scrape/tool.py): `_is_same_website` + the 5-scrape cap.

`_is_same_website` compares hosts from `_shared.host_of`, which must strip "www."
with removeprefix, never lstrip - lstrip treats "www." as a char SET {w,.} and
would turn "wisco.com" into "isco.com". The cap rejects re-scrapes and
over-budget calls (MAX_SCRAPE_CALLS).

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

    def test_rows(self):
        for a, b, same in [
            ("https://purvasparklingspring.com/", "https://purvasparklingspring.com/contact", True),
            ("https://www.purvasparklingspring.com", "https://purvasparklingspring.com", True),
            ("https://blog.purvasparklingspring.com", "https://purvasparklingspring.com", True),
            ("https://purvasparklingspring.com", "https://sobha.com", False),
            ("https://purvasparklingspring.com", "", False),
            # lstrip("www.") would make "wisco.com" into "isco.com": equal
            ("https://wisco.com", "https://isco.com", False),
        ]:
            with self.subTest(a=a, b=b):
                self.assertEqual(_is_same_website(a, b), same)


def _pages(*urls: str) -> dict:
    """product_data["pages"] shape: entry presence = successful scrape."""
    return {u: {"screenshot_url": ""} for u in urls}


class ScrapeCapLock(unittest.TestCase):

    def test_rows(self):
        full = _pages(*(f"https://site{i}.in" for i in range(MAX_SCRAPE_CALLS)))
        for label, url, pages, proceeds in [
            ("fresh and under the cap", "https://earthenambience.in",
             _pages("https://purvasparklingspring.com"), True),
            ("already scraped", "https://purvasparklingspring.com",
             _pages("https://purvasparklingspring.com"), False),
            ("over the cap", "https://brand-new.in", full, False),
        ]:
            with self.subTest(label):
                refusal = _reject_if_duplicate_or_over_cap(url, pages)
                self.assertEqual(refusal is None, proceeds)
                if refusal is not None:
                    self.assertFalse(refusal.success)


if __name__ == "__main__":
    unittest.main()
