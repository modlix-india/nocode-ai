"""Slice-0 invariant suite - characterizes EXISTING _next_action behavior.

Hard gate for the dependency rework (HLD/LLD doc §5.4, S0-1..S0-7): these lock
today's behavior BEFORE any behavior slice lands, so every later PR (enum
offers, marker deletion, engine conversion) is judged against this referee.
Asserts are behavior-not-bytes (D6): membership, order, tool named - never
prose wording. Invariant #6 goes through the REAL ``from_session`` lenient
path with raw legacy dicts, not the ``make_cctx`` fixture shortcut.
"""
from __future__ import annotations

import asyncio
import unittest

from app.agents.adzump.next_action import CampaignContext, _next_action
from app.agents.adzump.tools.launch import _launch_campaign
from tests.agents.adzump._fixtures import SAAS, make_cctx, make_session


def _entry(missing: list[str], prefix: str) -> str | None:
    """First missing-list entry for a field, matched by its stable prefix."""
    return next((m for m in missing if m.startswith(prefix)), None)


GOOGLE_DONE = {
    "platform": "Google Ads",
    "duration": "30 days",
    "budget": "$50/day",
    "parent_account": "111-222",
    "account": "333-444",
}
META_DONE = {**GOOGLE_DONE, "platform": "Meta", "fb_page": "pg-9"}


class NextActionInvariants(unittest.TestCase):
    """S0-1..S0-7 - the seven behaviors every rework PR must preserve."""

    # S0-1 · offer-once: a settled offer never re-enters the missing-list
    def test_settled_offer_absent_from_missing(self):
        rows = [
            ("analysis accepted", make_cctx(
                {"platform": "Google Ads"}, product=SAAS, attempted=True),
                "competitive analysis"),
            ("analysis declined", make_cctx(
                {"platform": "Google Ads", "competitive_analysis_declined": "true"},
                product=SAAS), "competitive analysis"),
            ("creatives settled", make_cctx(
                {"platform": "Meta"}, product=SAAS, creatives_resolved=True),
                "competitor creatives"),
        ]
        for label, cctx, prefix in rows:
            with self.subTest(label):
                self.assertIsNone(_entry(_next_action(cctx), prefix))

    # S0-2 · decline honored through the REAL from_session/predicate wiring
    def test_decline_honored_from_raw_session(self):
        rows = [
            ("analysis declined",
             {"platform": "Google Ads", "competitive_analysis_declined": "true"},
             {}, "competitive analysis"),
            ("creatives declined",
             {"platform": "Meta", "competitor_creatives_declined": "true"},
             {}, "competitor creatives"),
            ("empty fetch completed",
             {"platform": "Meta"},
             {"_competitor_creatives_fetched": True}, "competitor creatives"),
        ]
        for label, spec, extra, prefix in rows:
            with self.subTest(label):
                session = make_session(spec=spec, product=SAAS, **extra)
                cctx = CampaignContext.from_session(session)
                self.assertIsNone(_entry(_next_action(cctx), prefix))

    # S0-3 · Custom escape prescribes a typed ask, never re-renders chips
    def test_custom_escape_never_rechips(self):
        for field in ("duration", "budget"):
            with self.subTest(field):
                cctx = make_cctx({"platform": "Google Ads"}, product=SAAS,
                                 attempted=True, awaiting=field)
                line = _entry(_next_action(cctx), field)
                self.assertIsNotNone(line)
                self.assertIn("TYPE", line)
                self.assertNotIn("chip choices", line)

    # S0-4 · an accepted creatives offer prescribes the fetch, not a re-ask
    def test_accepted_offer_unlocks_fetch(self):
        rows = [
            ("rivals known", ["Lodha"], "fetch_competitor_creatives"),
            ("rivals unknown", [], "analyze_competitors"),
        ]
        for label, names, tool in rows:
            with self.subTest(label):
                cctx = make_cctx(
                    {"platform": "Meta", "competitor_creatives": "accepted"},
                    product=SAAS, competitor_names=names, last_user="Yes")
                line = _entry(_next_action(cctx), "competitor creatives")
                self.assertIsNotNone(line)
                self.assertIn(tool, line)
                self.assertNotIn("offer it ONCE", line)

    # S0-5 · review block structure across the three platform variants
    def test_review_block_shape(self):
        rows = [
            ("google", make_cctx(GOOGLE_DONE, product=SAAS, attempted=True),
             ("**Platform**", "Ready to launch", "launch_campaign"),
             ("**Facebook Page**", "**Instagram Account**")),
            ("meta+ig", make_cctx({**META_DONE, "ig_page": "ig-7"},
                                  product=SAAS, creatives_resolved=True),
             ("**Facebook Page**", "**Instagram Account**", "launch_campaign"),
             ("not linked",)),
            ("fb-only", make_cctx({**META_DONE, "ig_page_declined": "true"},
                                  product=SAAS, creatives_resolved=True),
             ("**Instagram Account**: not linked (Facebook only)",
              "launch_campaign"), ()),
        ]
        for label, cctx, present, absent in rows:
            with self.subTest(label):
                missing = _next_action(cctx)
                self.assertEqual(len(missing), 1)
                block = missing[0]
                self.assertTrue(block.startswith("review & publish"))
                for token in present:
                    self.assertIn(token, block)
                for token in absent:
                    self.assertNotIn(token, block)

    # S0-6 · in-flight legacy session resumes sanely through from_session
    def test_legacy_session_resume(self):
        session = make_session(
            last_user="hello again",
            spec={**META_DONE, "ig_page_declined": "true",
                  "competitor_creatives_declined": "true"},
            product=SAAS,
            _ig_offered=True,  # dead legacy marker rides along, ignored
            competitor_analysis={"competitors": [{"name": "Lodha"}]},
        )
        cctx = CampaignContext.from_session(session)
        self.assertTrue(cctx.competitor_creatives_offer_resolved)
        missing = _next_action(cctx)
        self.assertEqual(len(missing), 1)
        self.assertTrue(missing[0].startswith("review & publish"))

    # S0-7 · offers are never load-bearing: required asks proceed past declines
    def test_offers_never_load_bearing(self):
        cctx = make_cctx(
            {"platform": "Google Ads", "duration": "30 days",
             "competitive_analysis_declined": "true"}, product=SAAS)
        missing = _next_action(cctx)
        self.assertIsNotNone(_entry(missing, "budget"))
        self.assertIsNone(_entry(missing, "competitive analysis"))

    def test_launch_required_set_excludes_offers(self):
        # Both offers declined + every required field set: the launch guard
        # stack must pass the completeness check (offers are not required) and
        # stop at the consent gate - proving declines never block launch.
        spec = {**META_DONE, "ig_page_declined": "true",
                "competitor_creatives_declined": "true"}
        session = make_session(last_user="what about targeting?",
                               spec=spec, product=SAAS)
        result = asyncio.run(_launch_campaign(
            {}, {"session_context": session.context, "_session": session}))
        self.assertFalse(result.success)
        self.assertNotIn("missing required fields", result.error)
        self.assertIn("launch confirmation", result.error)


if __name__ == "__main__":
    unittest.main()
