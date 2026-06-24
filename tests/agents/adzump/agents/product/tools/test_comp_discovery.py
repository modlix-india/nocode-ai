"""comp_discovery pure helpers: _normalize_name (brand dedup), _is_specific_geography (geo hard-floor)."""
from __future__ import annotations

import unittest

from app.agents.adzump.agents.product.tools.comp_discovery import (
    _normalize_name, _is_specific_geography,
)


class NormalizeNameLock(unittest.TestCase):

    def test_brand_canonicalisation(self):
        cases = [
            ("Valmark CityVille", "valmark cityville"),
            ("Sumadhura Group", "sumadhura"),            # " group" suffix stripped
            ("Puravankara Pvt Ltd", "puravankara"),      # " pvt ltd" stripped
            ("Sattva-Songbird", "sattva songbird"),      # punctuation → space
            ("  Sobha  ", "sobha"),                      # strip + collapse
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(_normalize_name(raw), expected)


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


if __name__ == "__main__":
    unittest.main()
