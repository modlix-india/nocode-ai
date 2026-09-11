"""show_campaign_summary (rework slice 2): the code-rendered review card -
every bullet present across the three platform variants, IDs verbatim (never
'Linked'), audience=user delivery, and the completeness-gate refusal."""
from __future__ import annotations

import asyncio
import unittest

from app.agents.adzump.tools.summary import (
    _show_campaign_summary,
    render_summary_card,
)
from tests.agents.adzump._fixtures import SAAS, make_cctx, make_session

ACCOUNT_NAMES = {"1112223334": "Acme Manager", "5556667778": "Acme Ads",
                 "pg-9": "Acme FB", "ig-7": "Acme IG"}
GOOGLE_DONE = {
    "platform": "Google Ads", "location": "Bengaluru", "duration": "30 days",
    "budget": "$50/day", "parent_account": "1112223334", "account": "5556667778",
}
META_DONE = {**GOOGLE_DONE, "platform": "Meta", "fb_page": "pg-9"}

BULLETS = ("**Product**", "**Website**", "**Location**", "**Platform**",
           "**Duration**", "**Daily Budget**",
           "**Manager / Business Account**", "**Ad Account**",
           "**Competitors**")


class RenderSummaryCardTests(unittest.TestCase):
    """S2-1 · card correctness across the three platform variants."""

    def _card(self, spec, **kwargs):
        return render_summary_card(make_cctx(
            spec, product=SAAS, account_names=ACCOUNT_NAMES, **kwargs))

    def test_variant_rows(self):
        rows = [
            ("google", GOOGLE_DONE, {"attempted": True},
             ("Acme Manager (ID: 111-222-3334)",),
             ("**Facebook Page**", "**Instagram Account**")),
            ("meta+ig", {**META_DONE, "ig_page": "ig-7"},
             {"creatives_resolved": True},
             ("Acme FB (ID: pg-9)", "Acme IG (ID: ig-7)"), ("not linked",)),
            ("fb-only", META_DONE, {"creatives_resolved": True},
             ("**Instagram Account**: not linked (Facebook only)",), ()),
        ]
        for label, spec, kwargs, present, absent in rows:
            with self.subTest(label):
                card = self._card(spec, **kwargs)
                for bullet in BULLETS:
                    self.assertIn(bullet, card)
                for token in present:
                    self.assertIn(token, card)
                for token in absent:
                    self.assertNotIn(token, card)
                self.assertNotIn("Linked", card)  # IDs never degrade

    def test_google_cid_renders_dashed(self):
        card = self._card(GOOGLE_DONE, attempted=True)
        self.assertIn("Acme Manager (ID: 111-222-3334)", card)
        self.assertIn("Acme Ads (ID: 555-666-7778)", card)

    def test_competitors_line_rows(self):
        rows = [
            ("names", {"competitor_names": ["Lodha", "Sobha"]}, "Lodha, Sobha"),
            ("declined", {}, "declined"),
            ("none analyzed", {"attempted": True}, "none analyzed"),
            ("not analyzed", {}, "not analyzed"),
        ]
        for label, kwargs, expected in rows:
            with self.subTest(label):
                spec = dict(GOOGLE_DONE)
                if label == "declined":
                    spec["competitive_analysis"] = "declined"
                card = self._card(spec, **kwargs)
                self.assertIn(f"**Competitors**: {expected}", card)


class ShowCampaignSummaryTests(unittest.TestCase):
    """S2-2 · delivery: audience=user carries the card; incomplete spec is a
    refusal that never renders a partial card."""

    def _run(self, spec, **session_extra):
        session = make_session(spec=spec, product=SAAS, **session_extra)
        session.context["account_names"] = ACCOUNT_NAMES
        return asyncio.run(_show_campaign_summary({}, {"_session": session}))

    def test_complete_spec_renders_for_the_user(self):
        result = self._run({**GOOGLE_DONE, "competitive_analysis": "declined"})
        self.assertTrue(result.success)
        self.assertEqual(result.audience, "user")
        self.assertIn("Here's your campaign summary:", result.summary)
        self.assertIn("launch", result.model_summary)  # next step steered

    def test_incomplete_spec_refuses(self):
        result = self._run({"platform": "Google Ads"})
        self.assertFalse(result.success)
        self.assertIn("not complete", result.error)

    def test_no_session_refuses(self):
        result = asyncio.run(_show_campaign_summary({}, {}))
        self.assertFalse(result.success)


if __name__ == "__main__":
    unittest.main()
