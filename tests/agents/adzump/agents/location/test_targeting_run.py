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
    build_run_prompt,
    format_current_areas,
    resolve_country_geo_constant,
)


class CurrentAreasFormatTests(unittest.TestCase):
    def test_empty_areas_render_explicit_marker(self):
        self.assertIn("empty", format_current_areas([]).lower())

    def test_one_based_indexing(self):
        text = format_current_areas([{"name": "Andheri"}, {"name": "Juhu"}])
        self.assertIn("1. Andheri", text)
        self.assertIn("2. Juhu", text)
        # Zero-based numbering would be a silent bug - verify we don't have it.
        self.assertNotIn("0. Andheri", text)

    def test_unnamed_areas_use_marker(self):
        text = format_current_areas([{}, {"name": "Juhu"}])
        self.assertIn("1. (unnamed)", text)
        self.assertIn("2. Juhu", text)


class RunPromptTests(unittest.TestCase):
    def test_prompt_carries_profile_list_and_verbatim_request(self):
        product = {
            "product_name": "Purva Heights",
            "business_type": "Real Estate",
            "business_scale": "Local",
            "target_areas": [{"name": "Andheri"}],
            "summary": "Premium 3BHK apartments.",
        }
        prompt = build_run_prompt(product, "Mumbai, India", "IN", "add Juhu")
        self.assertIn("Purva Heights", prompt)
        self.assertIn("local", prompt)          # scale is normalized lowercase
        self.assertIn("Mumbai, India", prompt)
        self.assertIn("1. Andheri", prompt)
        self.assertIn('"""add Juhu"""', prompt)

    def test_prompt_truncates_long_summaries(self):
        prompt = build_run_prompt(
            {"summary": "x" * 1000}, "", "IN", "set targeting")
        self.assertNotIn("x" * 700, prompt)


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
                client = self._resolve(dict(place), country_name=name)
                if place.get("country_geo_constant") or not (name and place.get("country_code")):
                    client.assert_not_awaited()

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
