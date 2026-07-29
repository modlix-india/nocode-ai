"""Unit: tools/creatives.py - the fetch_competitor_creatives hard gates.

Gate order under test: Meta-only → user-consent → competitors-exist → fetch.
Consent is the harness enforcement of "never spend ad-library credits without
an explicit yes in the user's most recent message" (the launch_campaign
backstop shape); the Meta gate scopes the whole feature to the Meta flow.
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from app.agents.adzump.tools import creatives


def _session_with(last_user: str):
    """Minimal session double for _last_user_text (reads .messages)."""
    return SimpleNamespace(messages=[{"role": "user", "content": last_user}])


def _ctx(*, platform="Meta", last_user="Yes", competitors=None):
    session_ctx = {
        "campaign_spec": {"platform": platform},
        "competitor_analysis": {"competitors": (
            competitors if competitors is not None
            else [{"name": "Prestige", "url": "https://prestige.com"}]
        )},
    }
    return {"session_context": session_ctx, "_session": _session_with(last_user)}


def _run(ctx):
    with mock.patch.object(creatives.ci, "creatives_for_all",
                           new=mock.AsyncMock(return_value={})) as fetch:
        result = asyncio.run(creatives._fetch_competitor_creatives({}, ctx))
    return result, fetch


class MetaGateTests(unittest.TestCase):
    def test_google_flow_is_refused_before_any_fetch(self):
        result, fetch = _run(_ctx(platform="Google Ads"))
        self.assertFalse(result.success)
        self.assertIn("META flow only", result.error)
        fetch.assert_not_awaited()

    def test_no_platform_yet_is_refused(self):
        result, fetch = _run(_ctx(platform=""))
        self.assertFalse(result.success)
        fetch.assert_not_awaited()


class ConsentGateTests(unittest.TestCase):
    def test_replies_gate_correctly(self):
        cases = [
            ("Yes", True),                      # chip click
            ("yes, show me", True),
            ("show me their ads", True),        # verb form, no bare yes
            ("go ahead", True),
            ("", False),
            ("tell me about the budget", False),  # model jumping the gun
            ("no thanks", False),               # clear decline
            ("yesterday we discussed eyes", False),  # word boundary
        ]
        for last_user, allowed in cases:
            with self.subTest(last_user=last_user):
                result, fetch = _run(_ctx(last_user=last_user))
                self.assertEqual(result.success, allowed)
                if not allowed:
                    fetch.assert_not_awaited()
                    self.assertIn("Consent gate", result.error)

    def test_consent_error_names_the_reask_tool(self):
        result, _ = _run(_ctx(last_user="hmm"))
        self.assertIn("present_options", result.error)
        self.assertIn("competitor_creatives_declined", result.error)


class CompetitorsGateTests(unittest.TestCase):
    def test_consented_but_no_competitors_prescribes_analysis(self):
        result, fetch = _run(_ctx(competitors=[]))
        self.assertFalse(result.success)
        self.assertIn("analyze_competitors", result.error)
        fetch.assert_not_awaited()


class FetchMarkerTests(unittest.TestCase):
    def test_completed_fetch_sets_the_marker_even_with_zero_creatives(self):
        # The marker (not the creative lists) resolves the consent offer - a
        # zero-ad result must not re-open the offer every turn.
        ctx = _ctx()
        result, _ = _run(ctx)
        self.assertTrue(result.success)
        self.assertTrue(ctx["session_context"]["_competitor_creatives_fetched"])

    def test_failed_fetch_leaves_the_marker_unset(self):
        ctx = _ctx()
        with mock.patch.object(creatives.ci, "creatives_for_all",
                               new=mock.AsyncMock(side_effect=RuntimeError("boom"))):
            result = asyncio.run(creatives._fetch_competitor_creatives({}, ctx))
        self.assertFalse(result.success)
        self.assertNotIn("_competitor_creatives_fetched", ctx["session_context"])


if __name__ == "__main__":
    unittest.main()
