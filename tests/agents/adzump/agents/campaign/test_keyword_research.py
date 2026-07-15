"""Unit tests for the keyword_research orchestrator
(app/agents/adzump/agents/campaign/tools/google/keyword_research.py).

Covers _resolve_funnels — the single place that turns the user's consent-step answer into
the ad groups we build — and _resolve_geo's defensive read of product_data["place"].
"""
# regression: an unknown/absent funnel choice must fall back to the plan we showed the
# user, never to an arbitrary funnel; a null `place` must not crash the run.
from __future__ import annotations

import asyncio
import unittest

from app.agents.adzump.agents.campaign.tools.google.keyword_research import (
    _resolve_funnels,
    _resolve_geo,
)

_ALL = ["brand", "generic"]


class ResolveFunnelsTests(unittest.TestCase):
    # spec["funnels"] as it may land -> the ad groups to build
    CASES = [
        # the user hasn't chosen yet -> build the plan we showed them
        (None, _ALL, "absent"),
        ([], _ALL, "empty"),
        # canonical, from set_campaign_spec
        (["brand", "generic"], _ALL, "canonical list"),
        (["brand"], ["brand"], "narrowed to brand"),
        (["generic"], ["generic"], "narrowed to generic"),
        # chip labels, verbatim from present_options
        ("Both Brand & Generic", _ALL, "chip: both"),
        ("Brand only", ["brand"], "chip: brand only"),
        ("Generic only", ["generic"], "chip: generic only"),
        # tolerated shapes
        ("brand,generic", _ALL, "csv"),
        (["BRAND"], ["brand"], "uppercase"),
        (["  generic  "], ["generic"], "whitespace"),
        # unknown names are dropped, never guessed
        (["nonsense"], _ALL, "unknown -> plan"),
        (["brand", "nonsense"], ["brand"], "partial unknown -> keep the known"),
        # order + dedup
        (["generic", "brand"], ["generic", "brand"], "order preserved"),
        (["brand", "brand"], ["brand"], "deduped"),
    ]

    def test_table(self):
        for raw, expected, label in self.CASES:
            with self.subTest(label):
                self.assertEqual(_resolve_funnels({"funnels": raw}), expected)

    def test_missing_key_entirely(self):
        self.assertEqual(_resolve_funnels({}), _ALL)


class ResolveGeoTests(unittest.TestCase):
    """place can round-trip from persisted JSON as an explicit null."""

    def _geo(self, session_ctx: dict) -> dict:
        return asyncio.run(_resolve_geo(session_ctx, {"client_code": "c"}))

    def test_null_place_does_not_crash(self):
        # A setdefault would hand back None here and the .get below would raise.
        ctx = {"product_data": {"place": None}, "campaign_spec": {}}
        geo = self._geo(ctx)
        self.assertEqual(geo["geo_target_constants"], [])  # unresolved -> Planner default
        self.assertIsInstance(ctx["product_data"]["place"], dict)  # normalised in place

    def test_country_geo_constant_is_read_not_re_resolved(self):
        ctx = {
            "product_data": {"place": {"country_geo_constant": "geoTargetConstants/2826",
                                       "country_code": "GB"}},
            "campaign_spec": {},
        }
        geo = self._geo(ctx)
        self.assertEqual(geo["geo_target_constants"], ["geoTargetConstants/2826"])
        self.assertEqual(geo["gl"], "GB")


if __name__ == "__main__":
    unittest.main()
