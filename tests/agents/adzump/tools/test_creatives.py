"""Unit: tools/creatives.py - the fetch_competitor_creatives hard gates.

Gate order: Meta-only -> user-consent -> competitors-exist -> fetch. Consent is
the harness enforcement of "never spend ad-library credits without an explicit
yes in the user's most recent message"; the completion marker (not the creative
lists) is what resolves the consent offer.
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from app.agents.adzump.tools import creatives


def _ctx(*, platform="Meta", last_user="Yes", competitors=None):
    session_ctx = {
        "campaign_spec": {"platform": platform},
        "competitor_analysis": {"competitors": (
            competitors if competitors is not None
            else [{"name": "Prestige", "url": "https://prestige.com"}]
        )},
    }
    session = SimpleNamespace(messages=[{"role": "user", "content": last_user}])
    return {"session_context": session_ctx, "_session": session}


def _run(ctx, fetch=None):
    with mock.patch.object(creatives.ci, "creatives_for_all",
                           new=fetch or mock.AsyncMock(return_value={})) as fetched:
        result = asyncio.run(creatives._fetch_competitor_creatives({}, ctx))
    return result, fetched


class FetchCompetitorCreativesTests(unittest.TestCase):
    def test_gates_then_marker(self):
        for name, platform in [("google flow", "Google Ads"), ("no platform yet", "")]:
            with self.subTest(meta_gate=name):
                result, fetch = _run(_ctx(platform=platform))
                self.assertFalse(result.success)
                fetch.assert_not_awaited()
        for last_user, allowed in [
            ("Yes", True), ("yes, show me", True),
            ("show me their ads", True),                # verb form, no bare yes
            ("go ahead", True),
            ("", False),
            ("tell me about the budget", False),        # model jumping the gun
            ("no thanks", False),                       # clear decline
            ("yesterday we discussed eyes", False),     # word boundary
        ]:
            with self.subTest(consent=last_user or repr(last_user)):
                result, fetch = _run(_ctx(last_user=last_user))
                self.assertEqual(result.success, allowed)
                if not allowed:
                    fetch.assert_not_awaited()
                    self.assertIn("Consent gate", result.error)
                    # the refusal names the re-ask tool + tagged field
                    self.assertIn("present_options", result.error)
                    self.assertIn("competitor_creatives_declined", result.error)
        with self.subTest("consented but no competitors prescribes analysis"):
            result, fetch = _run(_ctx(competitors=[]))
            self.assertFalse(result.success)
            self.assertIn("analyze_competitors", result.error)
            fetch.assert_not_awaited()
        with self.subTest("completed fetch sets the marker even with zero creatives"):
            ctx = _ctx()
            result, _ = _run(ctx)
            self.assertTrue(result.success)
            self.assertTrue(ctx["session_context"]["_competitor_creatives_fetched"])
        with self.subTest("failed fetch leaves the marker unset"):
            ctx = _ctx()
            result, _ = _run(ctx, fetch=mock.AsyncMock(side_effect=RuntimeError("boom")))
            self.assertFalse(result.success)
            self.assertNotIn("_competitor_creatives_fetched", ctx["session_context"])


if __name__ == "__main__":
    unittest.main()
