"""targeting_run helpers - the prompt the model plans from, list rendering.

The agent's accuracy depends on this rendering: a wrong index map breaks the
model's ability to turn 'the second area' into delete_location(index=2).
build_run_result gating is covered end-to-end in test_agent (it needs the
run/session interplay); the pure rendering contracts are locked here.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.agents.location.targeting_run import (
    format_current_areas,
    resolve_country_geo_constant,
)


class CurrentAreasFormatTests(unittest.TestCase):
    def test_rows(self):
        # 1-based, so "the second area" maps to delete_location(index=2)
        for label, areas, present, absent in [
            ("no areas: an explicit marker", [], ["empty"], []),
            ("one-based numbering", [{"name": "Andheri"}, {"name": "Juhu"}],
             ["1. Andheri", "2. Juhu"], ["0. Andheri"]),
            ("an unnamed area keeps its number", [{}, {"name": "Juhu"}],
             ["1. (unnamed)", "2. Juhu"], []),
        ]:
            with self.subTest(label):
                text = format_current_areas(areas).lower()
                for token in present:
                    self.assertIn(token.lower(), text)
                for token in absent:
                    self.assertNotIn(token.lower(), text)


class CountryGeoConstantTests(unittest.TestCase):
    _SUGGEST = {"geoTargetConstantSuggestions": [
        {"geoTargetConstant": {"resourceName": "geoTargetConstants/2840",
                               "targetType": "City", "name": "Columbus"}},
        {"geoTargetConstant": {"resourceName": "geoTargetConstants/2841",
                               "targetType": "Country", "name": "United States"}},
    ]}

    def _resolve(self, place, country_name="United States", suggest=None):
        client = mock.AsyncMock(return_value=suggest if suggest is not None else self._SUGGEST)
        with mock.patch(
            "app.agents.adzump.adapters.google.client.google_ads_client.suggest_geo_targets",
            client,
        ):
            asyncio.run(resolve_country_geo_constant(place, country_name, "CL1", {}))
        return client

    def test_stamps_country_typed_constant(self):
        place = {"country_code": "US"}
        self._resolve(place)
        self.assertEqual(place["country_geo_constant"], "geoTargetConstants/2841")

    def test_skips_when_already_resolved_or_inputs_missing(self):
        cases = [
            ({"country_code": "US", "country_geo_constant": "geoTargetConstants/1"}, "United States"),
            ({"country_code": "US"}, ""),      # no country name
            ({}, "United States"),              # no country_code
        ]
        for place, name in cases:
            with self.subTest(place=place, name=name):
                self._resolve(dict(place), country_name=name).assert_not_awaited()

    def test_lookup_failure_never_raises(self):
        place = {"country_code": "US"}
        client = mock.AsyncMock(side_effect=RuntimeError("api down"))
        with mock.patch(
            "app.agents.adzump.adapters.google.client.google_ads_client.suggest_geo_targets",
            client,
        ):
            asyncio.run(resolve_country_geo_constant(place, "United States", "CL1", {}))
        self.assertNotIn("country_geo_constant", place)

    def test_no_country_suggestion_leaves_unset(self):
        place = {"country_code": "US"}
        self._resolve(place, suggest={"geoTargetConstantSuggestions": [
            {"geoTargetConstant": {"resourceName": "geoTargetConstants/9", "targetType": "City"}},
        ]})
        self.assertNotIn("country_geo_constant", place)


if __name__ == "__main__":
    unittest.main()
