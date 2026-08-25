"""Unit tests for the keyword_research orchestrator
(app/agents/adzump/agents/campaign/tools/google/keyword_research.py).

Covers resolve_theme_ids — the seam that turns the user's chosen ad groups into the
keyword themes we run — and _resolve_geo's defensive read of product_data["place"].
"""

# regression: an unknown/absent ad-group choice must fall back to the plan we showed the
# user, never to an arbitrary theme; a null `place` must not crash the run.
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.agents.campaign.craft import keyword_review_block
from app.agents.adzump.agents.campaign.google.keyword import manage_tools
from app.agents.adzump.agents.campaign.google.keyword.agent import KeywordResearchAgent
from app.agents.adzump.agents.campaign.google.keyword.models import (
    KeywordSet,
    NegativeKeyword,
    OptimizedKeyword,
)
from app.agents.adzump.agents.campaign.google.keyword.themes import resolve_theme_ids
from app.agents.adzump.agents.campaign.models import (
    SESSION_KEY,
    CampaignBuild,
    Channel,
    SearchBuild,
    build_gaps,
    build_missing,
    is_build_complete,
)
from app.agents.adzump.agents.campaign.models import keyword_research as saved_keywords
from app.agents.adzump.agents.campaign.tools.google import keyword_research as kr
from app.agents.adzump.agents.campaign.tools.google.keyword_research import (
    _resolve_geo,
)
from app.agents.adzump.agents.campaign.tools.google.keyword_update import _apply_edit
from app.agents.adzump.next_action import _next_action
from tests.agents.adzump._fixtures import make_cctx

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
                self.assertEqual(resolve_theme_ids({"ad_groups": raw}), expected)

    def test_missing_key_entirely(self):
        self.assertEqual(resolve_theme_ids({}), _ALL)


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


class UnfinishedBuildRoutingTests(unittest.TestCase):
    """A build that ran but did not finish must never be answered by building again: the
    rebuild carries every ad group that has keywords forward untouched, so it would loop."""

    SPEC = {
        "platform": "Google Ads",
        "channel": "SEARCH",
        "ad_groups": "both",
        "duration": "30 days",
        "budget": "10,000/day",
        "parent_account": "P",
        "account": "A",
        "location": "Hyderabad",
        "summary_confirmed": "true",
        "competitive_analysis_declined": "true",
    }
    PRODUCT = {
        "name": "Acme",
        "industry": "saas",
        "target_areas": [{"name": "Hyderabad", "google": "1007751"}],
    }

    def ctx(self, themes):
        build = CampaignBuild(
            channel=Channel.SEARCH,
            search=SearchBuild(keyword_research={"themes": themes}),
        )
        return {
            SESSION_KEY: build.model_dump(mode="json"),
            "campaign_spec": dict(self.SPEC),
        }

    @staticmethod
    def _set(label, status):
        return {"label": label, "status": status, "positives": [{"keyword": "a"}]}

    def prescriptions(self, ctx):
        cctx = make_cctx(
            dict(self.SPEC),
            product=self.PRODUCT,
            summary_confirmed=True,
            build_done=is_build_complete(ctx),
            build_gaps=build_gaps(ctx),
        )
        return list(_next_action(cctx))

    def test_a_partial_ad_group_routes_to_manage_not_a_rebuild(self):
        ctx = self.ctx(
            {
                "brand": self._set("Brand", "partial"),
                "generic": self._set("Generic", "complete"),
            }
        )
        self.assertFalse(is_build_complete(ctx))
        line = "\n".join(self.prescriptions(ctx))
        self.assertIn("Brand has no negatives", line)
        self.assertIn("manage_keywords", line)
        self.assertNotIn("prepare_campaign_review tool", line)

    def test_an_ad_group_with_no_keywords_routes_to_manage_not_a_rebuild(self):
        # Only the manage agent can create one, through research_ad_group.
        ctx = self.ctx({"brand": self._set("Brand", "complete")})
        self.assertFalse(is_build_complete(ctx))
        line = "\n".join(self.prescriptions(ctx))
        self.assertIn("Generic has no keywords", line)
        self.assertIn("manage_keywords", line)
        self.assertNotIn("prepare_campaign_review", line)

    def test_the_prescription_never_puts_words_in_the_users_mouth(self):
        # It used to name an ad group itself, and the model researched THAT one instead of
        # the one the user had just clicked. Their message must travel verbatim.
        ctx = self.ctx(
            {
                "brand": self._set("Brand", "complete"),
                "generic": self._set("Generic", "partial"),
            }
        )
        line = "\n".join(self.prescriptions(ctx))
        self.assertIn("VERBATIM", line)
        self.assertNotIn("user_message=", line)

    def test_an_unfinished_ad_group_is_not_a_missing_slot(self):
        # prepare_campaign_review reports build_missing as "that step did not complete";
        # the slot did arrive, so a partial ad group must not surface there.
        ctx = self.ctx({"brand": self._set("Brand", "partial")})
        self.assertEqual(build_missing(ctx), ())
        self.assertTrue(build_gaps(ctx))

    def test_a_finished_build_has_no_gaps(self):
        ctx = self.ctx(
            {
                "brand": self._set("Brand", "complete"),
                "generic": self._set("Generic", "complete"),
            }
        )
        self.assertTrue(is_build_complete(ctx))
        self.assertEqual(build_gaps(ctx), ())


class ResearchAdGroupTests(unittest.IsolatedAsyncioTestCase):
    """The only way to create an ad group after the build. edit_keywords cannot, so a theme
    that failed research has nothing to write into until this runs."""

    def ctx(self, themes, failed=()):
        build = CampaignBuild(
            channel=Channel.SEARCH,
            search=SearchBuild(
                keyword_research={"themes": themes, "meta": {"failed": list(failed)}}
            ),
        )
        return {
            SESSION_KEY: build.model_dump(mode="json"),
            "campaign_spec": {
                "channel": "SEARCH",
                "platform": "google",
                "ad_groups": "brand,generic",
            },
            "kw_business_text": "b",
            "kw_customer_id": "1",
        }

    @staticmethod
    def tool_ctx(session_ctx):
        return {
            "session_context": session_ctx,
            "event_stream": mock.AsyncMock(),
            "auth": mock.MagicMock(),
        }

    @staticmethod
    def _complete(theme, label):
        return {
            "theme": theme,
            "label": label,
            "status": "complete",
            "positives": [{"keyword": f"{theme} a", "volume": 10}],
            "negatives": [{"keyword": f"{theme} n"}],
        }

    async def test_it_researches_a_failed_ad_group_and_clears_the_ghost_tab(self):
        ctx = self.ctx(
            {"generic": self._complete("generic", "Generic")}, failed=["brand"]
        )
        produced = KeywordSet(
            theme="brand",
            label="Brand",
            positives=[OptimizedKeyword(keyword="brigade group", volume=9900)],
            negatives=[NegativeKeyword(keyword="brigade careers")],
        )
        with mock.patch.object(
            KeywordResearchAgent, "research", new=mock.AsyncMock(return_value=produced)
        ):
            res = await manage_tools._research_ad_group(
                {"keyword_type": "brand"}, self.tool_ctx(ctx)
            )
        self.assertTrue(res.success, res.error)
        dump = saved_keywords(ctx)
        self.assertIn("brand", dump["themes"])
        # Left in meta it would render a second, failed tab beside the real one.
        self.assertEqual(dump["meta"]["failed"], [])
        self.assertTrue(is_build_complete(ctx))

    async def test_it_refuses_an_ad_group_that_already_has_keywords(self):
        ctx = self.ctx({"brand": self._complete("brand", "Brand")})
        with mock.patch.object(KeywordResearchAgent, "research") as research:
            res = await manage_tools._research_ad_group(
                {"keyword_type": "brand"}, self.tool_ctx(ctx)
            )
        self.assertFalse(res.success)
        self.assertIn("already has keywords", res.error)
        research.assert_not_called()

    async def test_it_refuses_an_unknown_ad_group(self):
        ctx = self.ctx({"generic": self._complete("generic", "Generic")})
        res = await manage_tools._research_ad_group(
            {"keyword_type": "nonsense"}, self.tool_ctx(ctx)
        )
        self.assertFalse(res.success)
        self.assertIn("Unknown ad group", res.error)

    def test_edit_keywords_cannot_create_an_ad_group(self):
        ctx = self.ctx(
            {"generic": self._complete("generic", "Generic")}, failed=["brand"]
        )
        ok, message = _apply_edit(
            {
                "action": "add",
                "keyword_type": "brand",
                "section": "positives",
                "keyword": "brigade group",
                "volume": 10,
            },
            ctx,
        )
        self.assertFalse(ok)
        self.assertIn("research_ad_group", message)


class ChatEditVolumeTests(unittest.IsolatedAsyncioTestCase):
    """A spoken edit must never overwrite a real volume with a fresh lookup. The panel sends
    one it already has; the model cannot, so only a keyword whose TEXT changes needs one."""

    def ctx(self):
        build = CampaignBuild(
            channel=Channel.SEARCH,
            search=SearchBuild(
                keyword_research={
                    "themes": {
                        "brand": {
                            "theme": "brand",
                            "label": "Brand",
                            "status": "complete",
                            "positives": [
                                {
                                    "keyword": "brigade cornerstone",
                                    "volume": 9900,
                                    "match_type": "PHRASE",
                                }
                            ],
                            "negatives": [{"keyword": "n"}],
                        }
                    },
                    "meta": {},
                }
            ),
        )
        return {
            SESSION_KEY: build.model_dump(mode="json"),
            "campaign_spec": {
                "channel": "SEARCH",
                "platform": "google",
                "ad_groups": "brand",
            },
            "product_data": {"product_name": "Brigade"},
        }

    async def _edit(self, ctx, edit):
        async def zero_fill(context, rows):  # worst case: the Planner knows nothing
            for row in rows:
                row["volume"] = 0

        tool_ctx = {
            "session_context": ctx,
            "event_stream": mock.AsyncMock(),
            "auth": mock.MagicMock(),
        }
        with (
            mock.patch.object(manage_tools, "fill_volumes", zero_fill),
            mock.patch(
                "app.agents.adzump.agents.campaign.tools.google.keyword_update._emit_panel",
                new=mock.AsyncMock(),
            ),
        ):
            return await manage_tools._edit_keywords({"edits": [edit]}, tool_ctx)

    async def test_an_edit_that_keeps_the_keyword_keeps_its_volume(self):
        ctx = self.ctx()
        res = await self._edit(
            ctx,
            {
                "action": "edit",
                "keyword_type": "brand",
                "section": "positives",
                "old_keyword": "brigade cornerstone",
                "keyword": "brigade cornerstone",
                "match_type": "EXACT",
            },
        )
        self.assertTrue(res.success, res.error)
        row = saved_keywords(ctx)["themes"]["brand"]["positives"][0]
        self.assertEqual(row["volume"], 9900)
        self.assertEqual(row["match_type"], "EXACT")

    async def test_a_rename_is_re_priced_and_does_not_inherit_the_old_volume(self):
        ctx = self.ctx()
        res = await self._edit(
            ctx,
            {
                "action": "edit",
                "keyword_type": "brand",
                "section": "positives",
                "old_keyword": "brigade cornerstone",
                "keyword": "brigade utopia",
            },
        )
        self.assertTrue(res.success, res.error)
        row = saved_keywords(ctx)["themes"]["brand"]["positives"][0]
        self.assertEqual(row["keyword"], "brigade utopia")
        self.assertNotEqual(row["volume"], 9900)
