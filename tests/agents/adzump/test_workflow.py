"""The journey engine's invariant suite (S0-1..S0-7 + S3).

The S0 rows were written against the retired if-chain and passed UNCHANGED
across the slice-3 conversion - they are the equivalence referee (D6:
membership, order, tool named - never prose wording). Invariant #6 goes
through the REAL ``from_session`` lenient path with raw legacy dicts, not the
``make_cctx`` fixture shortcut. S3 adds the registry-discipline lint and the
engine's one deliberate semantic: waiting (an ask in flight) blocks review.
"""
from __future__ import annotations

import asyncio
import unittest

from app.agents.adzump.workflow import NEW_CAMPAIGN, CampaignContext, missing_list
from app.agents.adzump.tools.launch import _launch_campaign
from app.agents.adzump.models import OfferResolution
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
                self.assertIsNone(_entry(missing_list(NEW_CAMPAIGN, cctx), prefix))

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
                self.assertIsNone(_entry(missing_list(NEW_CAMPAIGN, cctx), prefix))

    # S0-3 · chip asks invite typing; no Custom chip is ever prescribed (D13)
    def test_chip_asks_invite_typing_no_custom_chip(self):
        for field in ("duration", "budget"):
            with self.subTest(field):
                cctx = make_cctx({"platform": "Google Ads"}, product=SAAS,
                                 attempted=True)
                line = _entry(missing_list(NEW_CAMPAIGN, cctx), field)
                self.assertIsNotNone(line)
                self.assertIn("type your own", line)
                self.assertNotIn("Custom", line)

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
                line = _entry(missing_list(NEW_CAMPAIGN, cctx), "competitor creatives")
                self.assertIsNotNone(line)
                self.assertIn(tool, line)
                self.assertNotIn("offer it ONCE", line)

    # S0-5 → S2 · the review prescription is two tool calls; the card itself
    # is CODE-rendered (tools/summary.py) - no VERBATIM template remains.
    def test_review_block_shape(self):
        rows = [
            ("google", make_cctx(GOOGLE_DONE, product=SAAS, attempted=True)),
            ("meta+ig", make_cctx({**META_DONE, "ig_page": "ig-7"},
                                  product=SAAS, creatives_resolved=True)),
            ("fb-only", make_cctx({**META_DONE, "ig_page_declined": "true"},
                                  product=SAAS, creatives_resolved=True)),
        ]
        for label, cctx in rows:
            with self.subTest(label):
                missing = missing_list(NEW_CAMPAIGN, cctx)
                self.assertEqual(len(missing), 1)
                block = missing[0]
                self.assertTrue(block.startswith("review & publish"))
                for token in ("show_campaign_summary", "Ready to launch",
                              "launch_campaign"):
                    self.assertIn(token, block)
                self.assertNotIn("VERBATIM", block)  # template engine is dead

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
        self.assertIs(cctx.competitor_creatives_resolution,
                      OfferResolution.DECLINED)
        missing = missing_list(NEW_CAMPAIGN, cctx)
        self.assertEqual(len(missing), 1)
        self.assertTrue(missing[0].startswith("review & publish"))

    # S0-7 · offers are never load-bearing: required asks proceed past declines
    def test_offers_never_load_bearing(self):
        cctx = make_cctx(
            {"platform": "Google Ads", "duration": "30 days",
             "competitive_analysis_declined": "true"}, product=SAAS)
        missing = missing_list(NEW_CAMPAIGN, cctx)
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


class JourneyEngineTests(unittest.TestCase):
    """S3 · the engine semantics the if-chain could not express, plus the
    registry-discipline lint."""

    def test_waiting_ask_blocks_review(self):
        # Everything set except the creatives offer, whose ask is ON SCREEN:
        # the step is waiting - never re-prescribed, but the journey is NOT
        # complete, so review cannot fire over an unanswered ask (the retired
        # if-chain prescribed review here). The missing-section prompt must
        # not claim review-readiness either.
        from app.agents.adzump.prompt_sections import _missing_section
        cctx = make_cctx({**META_DONE, "ig_page": "ig-7"}, product=SAAS,
                         pending_ask="competitor_creatives")
        self.assertEqual(missing_list(NEW_CAMPAIGN, cctx), [])
        section = _missing_section([])
        self.assertNotIn("review", section)
        self.assertIn("pending on screen", section)

    def test_accepted_answer_beats_the_open_rail(self):
        # A capture can store ACCEPTED while the rail is still open (cctx is
        # built before the answered rail is reaped): the fetch is owed NOW -
        # the waiting gate must not swallow the said-YES prescription (the
        # retired chain checked ACCEPTED before the rail, in that order).
        cctx = make_cctx(
            {**META_DONE, "ig_page": "ig-7", "competitor_creatives": "accepted"},
            product=SAAS, competitor_names=["Rival"],
            pending_ask="competitor_creatives")
        missing = missing_list(NEW_CAMPAIGN, cctx)
        self.assertEqual(len(missing), 1)
        self.assertIn("fetch_competitor_creatives", missing[0])

    def test_upstream_ask_hides_dependents(self):
        # No product: every later step requires it, so the URL ask is the
        # ONLY line (the old early-return, now expressed as dependencies).
        missing = missing_list(NEW_CAMPAIGN, make_cctx({}, product={}))
        self.assertEqual(len(missing), 1)
        self.assertIn("analyze_product", missing[0])

    def test_registry_discipline(self):
        names = [step.name for step in NEW_CAMPAIGN.steps]
        self.assertEqual(len(names), len(set(names)), "step names must be unique")
        seen: set[str] = set()
        for step in NEW_CAMPAIGN.steps:
            for required in step.requires:
                self.assertIn(required, seen,
                              f"{step.name} requires {required} which is not "
                              "an earlier step")
            seen.add(step.name)
        # launch.py's required-field set must be journey steps (offers aren't
        # in it - declines never block launch).
        launch_required = {"platform", "duration", "budget",
                           "parent_account", "account"}
        self.assertTrue(launch_required <= set(names))


if __name__ == "__main__":
    unittest.main()
