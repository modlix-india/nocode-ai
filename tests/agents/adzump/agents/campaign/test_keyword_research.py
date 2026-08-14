"""Unit tests for the keyword_research orchestrator
(app/agents/adzump/agents/campaign/tools/google/keyword_research.py).

Covers _resolve_themes — the seam that turns the user's chosen ad groups into the
keyword themes we run — and _resolve_geo's defensive read of product_data["place"].
"""

# regression: an unknown/absent ad-group choice must fall back to the plan we showed the
# user, never to an arbitrary theme; a null `place` must not crash the run.
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.agents.campaign.craft import keyword_review_block
from app.agents.adzump.agents.campaign.google.keyword.models import (
    KeywordSet,
    NegativeKeyword,
    OptimizedKeyword,
)
from app.agents.adzump.agents.campaign.models import keyword_research as saved_keywords
from app.agents.adzump.agents.campaign.tools.google import keyword_research as kr
from app.agents.adzump.agents.campaign.tools.google.keyword_research import (
    _resolve_geo,
    _resolve_themes,
)

_ALL = ["brand", "generic"]


class ResolveThemesTests(unittest.TestCase):
    # spec["ad_groups"] as it may land -> the themes to run
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
                self.assertEqual(_resolve_themes({"ad_groups": raw}), expected)

    def test_missing_key_entirely(self):
        self.assertEqual(_resolve_themes({}), _ALL)


class ResolveGeoTests(unittest.TestCase):
    """place can round-trip from persisted JSON as an explicit null."""

    def _geo(self, session_ctx: dict) -> dict:
        return asyncio.run(_resolve_geo(session_ctx, {"client_code": "c"}))

    def test_null_place_does_not_crash(self):
        # A setdefault would hand back None here and the .get below would raise.
        ctx = {"product_data": {"place": None}, "campaign_spec": {}}
        geo = self._geo(ctx)
        self.assertEqual(
            geo["geo_target_constants"], []
        )  # unresolved -> Planner default
        self.assertIsInstance(ctx["product_data"]["place"], dict)  # normalised in place

    def test_country_geo_constant_is_read_not_re_resolved(self):
        ctx = {
            "product_data": {
                "place": {
                    "country_geo_constant": "geoTargetConstants/2826",
                    "country_code": "GB",
                }
            },
            "campaign_spec": {},
        }
        geo = self._geo(ctx)
        self.assertEqual(geo["geo_target_constants"], ["geoTargetConstants/2826"])
        self.assertEqual(geo["gl"], "GB")


if __name__ == "__main__":
    unittest.main()


class _ProgressiveHarness:
    """Drives _keyword_research with stubbed IO, capturing what the panel receives."""

    def __init__(self):
        self.emits: list[tuple[str, list]] = []
        self.session_ctx = {
            "campaign_spec": {
                "platform": "GOOGLE",
                "account": "1",
                "ad_groups": "brand,generic",
            },
            "product_data": {"product_name": "K"},
        }

    @staticmethod
    def kset(theme, n_pos, n_neg, status="complete"):
        return KeywordSet(
            theme=theme,
            label=theme.title(),
            status=status,
            positives=[
                OptimizedKeyword(keyword=f"{theme} kw {i}", volume=100)
                for i in range(n_pos)
            ],
            negatives=[
                NegativeKeyword(keyword=f"{theme} neg {i}") for i in range(n_neg)
            ],
        )

    def run(self, research):
        async def cap_craft(stream, cid, sctx):
            themes = (saved_keywords(sctx) or {}).get("themes") or {}
            self.emits.append(("full", sorted(themes)))

        async def cap_section(stream, cid, block):
            self.emits.append(
                ("section", [(t["key"], t["status"]) for t in block["tabs"]])
            )

        taxonomy = mock.MagicMock(
            model_dump=lambda: {
                "complete": True,
                "core_terms": [],
                "sibling_categories": [],
                "is_location_specific": True,
                "includes_informational_funnel": False,
                "primary_offering": "t",
            }
        )
        ctx = {
            "session_context": self.session_ctx,
            "auth": mock.MagicMock(),
            "event_stream": mock.AsyncMock(),
        }
        with (
            mock.patch.object(
                kr,
                "_resolve_geo",
                new=mock.AsyncMock(
                    return_value={
                        "geo_target_constants": [],
                        "hl": "en",
                        "gl": "IN",
                        "language": "x",
                    }
                ),
            ),
            mock.patch.object(
                kr,
                "derive_offering_taxonomy",
                new=mock.AsyncMock(return_value=taxonomy),
            ),
            mock.patch.object(kr, "resolve_url", return_value=""),
            mock.patch.object(kr, "_business_text", return_value="b"),
            mock.patch.object(kr, "_resolve_location", return_value=("D", [])),
            mock.patch.object(kr, "emit_campaign_craft", new=cap_craft),
            mock.patch.object(kr, "emit_section_update", new=cap_section),
            mock.patch.object(kr, "get_keyword_research_agent") as agent,
        ):
            agent.return_value.research = research
            return asyncio.run(kr._keyword_research({}, ctx))


class ProgressiveEmissionTests(unittest.TestCase):
    """An ad group reaches the panel when IT finishes, not when the slowest one does."""

    def test_a_finished_ad_group_is_shown_while_the_other_still_runs(self):
        h = _ProgressiveHarness()

        async def research(*, keyword_type, partial_sink=None, **kw):
            await asyncio.sleep(0.01 if keyword_type == "brand" else 0.05)
            return h.kset(keyword_type, 26, 32)

        res = h.run(research)
        self.assertTrue(res.success)
        # first emission carries brand alone; generic joins on the second
        self.assertEqual(h.emits[0], ("full", ["brand"]))
        self.assertEqual(h.emits[1][0], "section")
        self.assertEqual(
            sorted(h.emits[1][1]), [("brand", "complete"), ("generic", "complete")]
        )

    def test_a_timed_out_ad_group_keeps_the_keywords_it_already_picked(self):
        h = _ProgressiveHarness()

        async def research(*, keyword_type, partial_sink=None, **kw):
            await asyncio.sleep(0.01 if keyword_type == "brand" else 0.05)
            if keyword_type == "generic":
                # phases that finished are handed back before cancellation propagates
                partial_sink[keyword_type] = h.kset("generic", 25, 0, status="partial")
                raise asyncio.CancelledError()
            return h.kset(keyword_type, 26, 32)

        res = h.run(research)
        self.assertTrue(res.success)
        generic = saved_keywords(h.session_ctx)["themes"]["generic"]
        self.assertEqual(generic["status"], "partial")
        self.assertEqual(len(generic["positives"]), 25)  # real work, not discarded
        self.assertEqual(generic["negatives"], [])
        self.assertIn("partial", res.summary)

    def test_an_ad_group_that_picked_nothing_is_failed_not_empty(self):
        h = _ProgressiveHarness()

        async def research(*, keyword_type, partial_sink=None, **kw):
            await asyncio.sleep(0.01 if keyword_type == "brand" else 0.02)
            return h.kset(keyword_type, 0 if keyword_type == "generic" else 26, 32)

        res = h.run(research)
        self.assertTrue(res.success)
        self.assertIn(("generic", "failed"), h.emits[-1][1])
        self.assertNotIn("generic", saved_keywords(h.session_ctx)["themes"])

    def test_when_every_ad_group_fails_nothing_is_shown_or_stored(self):
        h = _ProgressiveHarness()

        async def research(*, keyword_type, partial_sink=None, **kw):
            return h.kset(keyword_type, 0, 0)

        res = h.run(research)
        self.assertFalse(res.success)
        self.assertEqual(h.emits, [])  # no misleading panel
        self.assertIsNone(saved_keywords(h.session_ctx))  # nothing persisted


class PanelStatusTests(unittest.TestCase):
    """Every ad group the user chose gets a tab — including ones still running or failed."""

    def test_pending_and_failed_ad_groups_get_their_own_tabs(self):
        dump = {
            "themes": {
                "brand": {
                    "theme": "brand",
                    "label": "Brand",
                    "status": "complete",
                    "positives": [{"keyword": "a", "volume": 1}],
                    "negatives": [],
                }
            },
            "meta": {"pending": ["generic"], "failed": ["local"]},
        }
        tabs = {t["key"]: t for t in keyword_review_block(dump)["tabs"]}
        self.assertEqual(tabs["brand"]["status"], "complete")
        self.assertEqual(tabs["generic"]["status"], "pending")
        self.assertEqual(tabs["local"]["status"], "failed")
        self.assertEqual(tabs["generic"]["sections"], [])  # nothing to edit yet
