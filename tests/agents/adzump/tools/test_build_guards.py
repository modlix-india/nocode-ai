"""The guards that stop a build from destroying itself
(tools/prepare_campaign_review.py, tools/keyword_management.py, tools/audience_management.py).
"""

# regression: a user asking for an audience change ("target people searching for X") was
# routed to manage_keywords on a Demand Gen campaign, which had no channel gate and returned
# nothing useful; the orchestrator then called prepare_campaign_review, which rebuilt the
# campaign from scratch and replaced a reviewed 6-segment audience with a different 3-segment
# one. Every step below is one of the doors that were open.
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.agents.campaign.models import (
    audience,
    keyword_research,
    set_audience,
    set_channel_controls,
    set_keyword_research,
)
from app.agents.adzump.agents.campaign.tools.google import (
    audience_targeting as at,
)
from app.agents.adzump.tools import (
    audience_management,
    keyword_management,
)
from app.agents.adzump.tools import (
    prepare_campaign_review as pcr,
)

_SIGNAL = {
    "kind": "IN_MARKET",
    "ref": "customers/1/inMarketSegments/1",
    "label": "Accounting Software",
    "source": "TAXONOMY",
    "rationale": "r",
    "path": ["Software", "Accounting Software"],
}


def _ctx(channel="Demand Gen"):
    return {
        "session_context": {
            "campaign_spec": {
                "platform": "GOOGLE",
                "channel": channel,
                "account": "1",
            },
            "product_data": {"product_name": "Zoho Books"},
        },
        "auth": mock.MagicMock(),
        "event_stream": mock.AsyncMock(),
        "session_id": "s1",
    }


def _built(ctx):
    set_audience(
        ctx["session_context"],
        {
            "signals": [_SIGNAL],
            "demographics": {},
            "meta": {},
            "dimension_groups": [[_SIGNAL["ref"]]],
        },
    )
    return ctx


def _run_prepare(ctx, produced=None):
    """Drive the tool with the sub-agent stubbed, capturing what create() was handed."""
    seen: dict = {}

    async def create(**kw):
        seen.update(kw)
        return produced

    with (
        mock.patch.object(pcr, "get_campaign_agent") as agent,
        mock.patch.object(pcr, "pre_emit_agent_started", new=mock.AsyncMock()),
    ):
        agent.return_value.create = create
        res = asyncio.run(pcr._prepare_campaign_review({}, ctx))
    return res, seen


class RebuildGuardTests(unittest.TestCase):
    """A built campaign is the user's reviewed work. Building again replaces it wholesale."""

    def test_a_complete_build_is_never_rebuilt(self):
        ctx = _built(_ctx())
        before = audience(ctx["session_context"])
        res, seen = _run_prepare(ctx)

        self.assertFalse(res.success)
        self.assertIn("ALREADY built", res.error)
        self.assertEqual(seen, {})  # the sub-agent never ran
        self.assertEqual(audience(ctx["session_context"]), before)

    def test_the_refusal_names_the_tools_that_should_have_been_called(self):
        res, _ = _run_prepare(_built(_ctx()))
        self.assertIn("manage_audience", res.error)
        self.assertIn("manage_keywords", res.error)

    def test_a_first_build_still_runs(self):
        ctx = _ctx()
        _, seen = _run_prepare(ctx)
        self.assertIn("campaign_spec", seen)

    def test_a_build_for_another_channel_does_not_block_this_one(self):
        # Demand Gen work is not Search work; switching channel must be able to build.
        ctx = _built(_ctx())
        ctx["session_context"]["campaign_spec"]["channel"] = "Search"
        _, seen = _run_prepare(ctx)
        self.assertIn("campaign_spec", seen)

    def test_a_partial_build_is_carried_into_the_retry(self):
        # channel_controls ran, audience did not. The retry must see the finished slot, or
        # every tool starts blind and redoes work that already succeeded.
        ctx = _ctx()
        set_channel_controls(ctx["session_context"], {"youtube_in_feed": True})
        _, seen = _run_prepare(ctx)
        self.assertIsNotNone(seen.get("build"))
        self.assertIsNotNone(seen["build"]["demand_gen"]["channel_controls"])


class ChannelGateTests(unittest.TestCase):
    """A manage tool for the wrong channel must refuse and name the right one."""

    def _keywords(self, ctx):
        return asyncio.run(
            keyword_management._manage_keywords({"user_message": "add more"}, ctx)
        )

    def _audience(self, ctx):
        return asyncio.run(
            audience_management._manage_audience(
                {"user_message": "target people searching for zoho alternatives"}, ctx
            )
        )

    def test_manage_keywords_refuses_on_demand_gen(self):
        res = self._keywords(_built(_ctx()))
        self.assertFalse(res.success)
        self.assertIn("no keywords", res.error)
        self.assertIn("manage_audience", res.error)

    def test_manage_keywords_refuses_when_search_has_no_research(self):
        res = self._keywords(_ctx(channel="Search"))
        self.assertFalse(res.success)

    def test_manage_keywords_runs_for_a_search_campaign_with_research(self):
        ctx = _ctx(channel="Search")
        set_keyword_research(ctx["session_context"], {"themes": {"brand": {}}})
        with mock.patch.object(keyword_management, "get_keyword_manage_agent") as agent:
            agent.return_value.handle = mock.AsyncMock(return_value="ran")
            self.assertEqual(self._keywords(ctx), "ran")

    def test_manage_audience_refuses_before_an_audience_exists(self):
        res = self._audience(_ctx())
        self.assertFalse(res.success)
        self.assertIn("no audience yet", res.error)
        self.assertIn("Do NOT rebuild", res.error)

    def test_manage_audience_runs_once_one_exists(self):
        with mock.patch.object(
            audience_management, "get_audience_manage_agent"
        ) as agent:
            agent.return_value.handle = mock.AsyncMock(return_value="ran")
            self.assertEqual(self._audience(_built(_ctx())), "ran")


_RESEARCH = {
    "channel": "SEARCH",
    "search": {"keyword_research": {"themes": {"brand": {"positives": []}}}},
}


class SearchFlowTests(unittest.TestCase):
    """The mirror of the Demand Gen bug: a Search campaign must never grow an audience."""

    def test_building_search_leaves_the_audience_untouched(self):
        ctx = _ctx(channel="Search")
        res, _ = _run_prepare(ctx, produced=_RESEARCH)
        self.assertTrue(res.success)
        self.assertIsNotNone(keyword_research(ctx["session_context"]))
        self.assertIsNone(audience(ctx["session_context"]))

    def test_the_audience_build_tool_skips_search_without_writing(self):
        # The campaign agent is offered every platform tool; each one gates itself.
        ctx = _ctx(channel="Search")
        res = asyncio.run(at._audience_targeting({}, ctx))
        self.assertTrue(res.success)
        self.assertTrue(res.data["skipped"])
        self.assertIsNone(audience(ctx["session_context"]))

    def test_an_audience_question_on_search_is_refused_not_built(self):
        # The failure that started this: a refusal here must not send the orchestrator
        # looking for another tool that would rebuild the campaign.
        ctx = _ctx(channel="Search")
        set_keyword_research(ctx["session_context"], {"themes": {"brand": {}}})
        res = asyncio.run(
            audience_management._manage_audience(
                {"user_message": "who does this reach?"}, ctx
            )
        )
        self.assertFalse(res.success)
        self.assertIn("Do NOT rebuild", res.error)
        self.assertIsNone(audience(ctx["session_context"]))

    def test_a_built_search_campaign_is_not_rebuilt_either(self):
        ctx = _ctx(channel="Search")
        # The choice this build answered. Without it the plan is both ad groups, and a build
        # holding only brand is correctly unfinished rather than already built.
        ctx["session_context"]["campaign_spec"]["ad_groups"] = "brand"
        set_keyword_research(ctx["session_context"], {"themes": {"brand": {}}})
        before = keyword_research(ctx["session_context"])
        res, seen = _run_prepare(ctx, produced=_RESEARCH)
        self.assertFalse(res.success)
        self.assertEqual(seen, {})
        self.assertEqual(keyword_research(ctx["session_context"]), before)


if __name__ == "__main__":
    unittest.main()
