"""Lock #3 (partial) — business_storage pure helpers feeding the ds launch write.

Covers `_normalize_url` (the storage key — http→https, www-strip, trailing-slash)
and `_build_location_object` (the legacy ds-v1 location shape: map-confirmed →
user-typed → scraped precedence). The full `_build_full_record` golden +
`parse_location_update` are a focused follow-up (need a constructed session_ctx).

Run:
    cd nocode-ai && ./venv/bin/python -m unittest \\
        tests.agents.adzump.test_business_storage -v
"""

from __future__ import annotations

import unittest

from app.agents.adzump.services.business_storage import (
    _normalize_url, _build_location_object,
)


class NormalizeUrlLock(unittest.TestCase):

    def test_canonicalises_for_storage_key(self):
        cases = [
            ("http://www.PurvaSparklingSpring.com/villas/", "https://purvasparklingspring.com/villas"),
            ("https://sobha.com", "https://sobha.com"),
            ("http://x.com/", "https://x.com"),
            ("https://www.earthenambience.in/", "https://earthenambience.in"),
            ("", ""),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(_normalize_url(raw), expected)


class BuildLocationObjectLock(unittest.TestCase):

    def test_map_confirmed_wins_with_coords(self):
        loc_meta = {"address": "Sarjapur Road, Bengaluru", "lat": 12.9, "lng": 77.7}
        out = _build_location_object(loc_meta, {"location": "Bengaluru"}, {})
        self.assertEqual(out["product_location"], "Sarjapur Road, Bengaluru")
        self.assertEqual(out["product_coordinates"], {"lng": 77.7, "lat": 12.9})
        self.assertEqual(out["area_location"], "")

    def test_spec_then_scraped_fallback_no_coords(self):
        # No map address → user-typed spec.location wins; no lat/lng → coords None.
        out = _build_location_object({}, {"location": "Whitefield"}, {"location": "from-site"})
        self.assertEqual(out["product_location"], "Whitefield")
        self.assertIsNone(out["product_coordinates"])
        # spec empty too → scraped product.location.
        out2 = _build_location_object({}, {}, {"location": "Hosur Road"})
        self.assertEqual(out2["product_location"], "Hosur Road")


if __name__ == "__main__":
    unittest.main()
