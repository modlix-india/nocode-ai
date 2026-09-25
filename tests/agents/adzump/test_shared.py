"""Unit: app/agents/adzump/_shared.py - normalize_business_url, the storage key
(http→https, www-strip, trailing-slash)."""

import unittest

from app.agents.adzump._shared import normalize_business_url


class NormalizeUrlLock(unittest.TestCase):

    def test_canonicalises_for_storage_key(self):
        cases = [
            ("http://www.PurvaSparklingSpring.com/villas/", "https://purvasparklingspring.com/villas"),
            ("https://sobha.com", "https://sobha.com"),
            ("http://x.com/", "https://x.com"),
            ("https://www.earthenambience.in/", "https://earthenambience.in"),
            ("www.Nike.com/air/", "https://nike.com/air"),              # scheme-less
            ("https://x.com/p/?utm_source=ad#top", "https://x.com/p"),  # tracking params
            ("", ""),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_business_url(raw), expected)


if __name__ == "__main__":
    unittest.main()
