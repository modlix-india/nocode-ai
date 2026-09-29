"""show_campaign_summary (rework slice 2): the code-rendered review card -
every bullet present across the three platform variants, IDs verbatim (never
'Linked'), audience=user delivery, and the refusal until the journey is complete."""
from __future__ import annotations

import asyncio
import unittest

from app.agents.adzump.tools.summary import (
    _show_campaign_summary,
    render_summary_card,
)
from tests.agents.adzump._fixtures import RE, SAAS, make_actx, make_session

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
        return render_summary_card(make_actx(
            spec, product=SAAS, account_names=ACCOUNT_NAMES, **kwargs))

    def test_variant_rows(self):
        rows = [
            ("google", GOOGLE_DONE, {"attempted": True, "competitor_names": ["Lodha", "Sobha"]},
             ("Acme Manager (ID: 111-222-3334)", "Acme Ads (ID: 555-666-7778)", "Lodha", "Sobha"),
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


class ShowCampaignSummaryTests(unittest.TestCase):
    """S2-2 · delivery: audience=user carries the card; incomplete spec is a
    refusal that never renders a partial card."""

    def _run(self, spec, product=SAAS, **session_extra):
        session = make_session(spec=spec, product=product, **session_extra)
        session.context["account_names"] = ACCOUNT_NAMES
        return asyncio.run(_show_campaign_summary({}, {"_session": session}))

    def test_complete_spec_renders_for_the_user(self):
        mapped = {**SAAS, "target_areas": [{"name": "Bengaluru", "google": {"id": 1}}]}
        result = self._run({**GOOGLE_DONE, "competitive_analysis": "declined"}, product=mapped)
        self.assertTrue(result.success)
        self.assertEqual(result.audience, "user")
        self.assertIn("**Ad Account**", result.summary)  # the card, not a partial
        self.assertIn("launch", result.model_summary)  # next step steered

    def test_incomplete_journey_refuses(self):
        rows = [
            ("fields missing", {"platform": "Google Ads"}, SAAS),
            # every field answered, but the target areas are not mapped yet
            ("target areas unmapped", {**GOOGLE_DONE, "competitive_analysis": "declined"},
             {**RE, "target_areas": [{"name": "Whitefield"}]}),
        ]
        for case, spec, product in rows:
            with self.subTest(case):
                result = self._run(spec, product=product)
                self.assertFalse(result.success)
                self.assertIn("not complete", result.error)


if __name__ == "__main__":
    unittest.main()
