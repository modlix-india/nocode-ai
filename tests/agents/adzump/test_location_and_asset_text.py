"""Lock #7 — small pure helpers:
  - `_is_real_estate` / `_detected_location`  (tools/location.py)
  - `_compose_asset_request_text`             (scrape/assets.py)

`_is_real_estate` gates the real-estate conditional (our first vertical);
`_detected_location` normalises the str/dict location shapes; the asset-request
text is the user-facing prompt when logo/creatives are missing.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest \\
        tests.agents.adzump.test_location_and_asset_text -v
"""

from __future__ import annotations

import unittest

from app.agents.adzump.tools.location import _is_real_estate, _detected_location
from app.agents.adzump.agents.product.tools.scrape.assets import (
    _compose_asset_request_text,
)


class IsRealEstateLock(unittest.TestCase):

    def test_real_estate_business_types(self):
        for bt in ["Real Estate Developer", "Luxury Villas", "3BHK Apartments",
                   "Residential Township", "Property Management", "realty group"]:
            with self.subTest(bt=bt):
                self.assertTrue(_is_real_estate(bt))

    def test_non_real_estate(self):
        for bt in ["SaaS platform", "Restaurant chain", "Law firm", "", None]:
            with self.subTest(bt=bt):
                self.assertFalse(_is_real_estate(bt))


class DetectedLocationLock(unittest.TestCase):

    def test_str_dict_and_empty_shapes(self):
        self.assertEqual(_detected_location({"location": "Bengaluru"}), "Bengaluru")
        self.assertEqual(
            _detected_location({"location": {"location": "Whitefield, Bangalore"}}),
            "Whitefield, Bangalore")
        self.assertEqual(_detected_location({"location": "  Pune  "}), "Pune")  # stripped
        self.assertEqual(_detected_location({"location": {}}), "")
        self.assertEqual(_detected_location({"location": None}), "")
        self.assertEqual(_detected_location({}), "")


class AssetRequestTextLock(unittest.TestCase):

    def test_logo_only(self):
        t = _compose_asset_request_text(True, [])
        self.assertIn("brand logo", t)
        self.assertNotIn("I picked some ad images", t)   # no creatives sentence

    def test_creatives_only_oxford_join(self):
        t = _compose_asset_request_text(False, ["hero", "amenity", "floor_plan"])
        self.assertIn("a hero shot, an amenity / lifestyle photo, and a floor plan", t)
        self.assertIn("I picked some ad images, but I'm missing", t)

    def test_two_creatives_and_join(self):
        t = _compose_asset_request_text(False, ["hero", "amenity"])
        self.assertIn("a hero shot and an amenity / lifestyle photo", t)

    def test_both_logo_and_creatives(self):
        t = _compose_asset_request_text(True, ["floor_plan"])
        self.assertIn("brand logo", t)
        self.assertIn("I'm also missing a floor plan", t)

    def test_nothing_missing_is_empty(self):
        self.assertEqual(_compose_asset_request_text(False, []), "")


if __name__ == "__main__":
    unittest.main()
