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


def _ctx(*, platform="Meta", last_user="Yes", competitors=None, messages=None):
    session_ctx = {
        "campaign_spec": {"platform": platform},
        "competitor_analysis": {"competitors": (
            competitors if competitors is not None
            else [{"name": "Prestige", "url": "https://prestige.com"}]
        )},
    }
    session = SimpleNamespace(
        messages=messages if messages is not None
        else [{"role": "user", "content": last_user}])
    return {"session_context": session_ctx, "_session": session}


# The Anthropic-format history mid-turn: the human's Yes, then a tool call and
# its result - which is appended as a role="user" message (session.append_tool_results).
_MID_TURN_MESSAGES = [
    {"role": "user", "content": "Yes"},
    {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "analyze_competitors", "input": {}}]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "Found 5 competitors"}]},
]


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
                    self.assertIn('field "competitor_creatives"', result.error)
                    # the customer's tool row gets calm copy, never the steering
                    self.assertIn("go-ahead", result.display_error)
                    self.assertNotIn("present_options", result.to_display_text())
        with self.subTest("stored acceptance passes the gate (stored-ok exception)"):
            # HLD/LLD §4.5: the fetch is metered but internal and reversible -
            # a stored yes must not expire because a digression moved the
            # latest message (live: the consented fetch died on the way to
            # the analyze step).
            ctx = _ctx(last_user="tell me about the budget")
            ctx["session_context"]["campaign_spec"]["competitor_creatives"] = "accepted"
            result, fetch = _run(ctx)
            self.assertTrue(result.success)
        with self.subTest("consent survives a tool result later in the turn"):
            # The gate's own "run analyze_competitors NOW, then call fetch AGAIN
            # in this same turn" must be satisfiable (incident: LastUserTextTests).
            result, fetch = _run(_ctx(messages=list(_MID_TURN_MESSAGES)))
            self.assertTrue(result.success)
            fetch.assert_awaited()
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

    def test_already_fetched_entries_are_skipped(self):
        """Session-level cache: only the entry WITHOUT creatives is fetched
        when one more competitor is added (live 2026-09-04: Purva re-spent
        credits because the 404ing shared store was the only guard).
        Fetched-empty ([] = honest 'No ads found') also skips."""
        competitors = [
            {"name": "Purva", "url": "https://purvasparklingspring.com",
             "creatives": [{"creativeId": "a1"}], "totalCreatives": 1,
             "activeCreatives": 1},
            {"name": "Shriram", "url": "https://shriramnewlaunch.com",
             "creatives": [], "totalCreatives": 0, "activeCreatives": 0},
            {"name": "Nambiar", "url": "https://nambiarprojects.com"},
        ]
        result, fetch = _run(_ctx(competitors=competitors))
        self.assertTrue(result.success)
        fetched_names = [p.name for p in fetch.await_args.args[0]]
        self.assertEqual(fetched_names, ["Nambiar"])

    def test_all_fetched_short_circuits_without_spend(self):
        competitors = [{"name": "Purva", "url": "https://x.com",
                        "creatives": [], "totalCreatives": 0,
                        "activeCreatives": 0}]
        ctx = _ctx(competitors=competitors)
        result, fetch = _run(ctx)
        self.assertTrue(result.success)
        fetch.assert_not_awaited()
        self.assertTrue(ctx["session_context"]["_competitor_creatives_fetched"])


if __name__ == "__main__":
    unittest.main()
