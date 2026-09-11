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
            # Live 2026-09-08: a parallel analyze+fetch raced; this refusal must
            # never mark the offer resolved or the owed fetch evaporates.
            ctx = _ctx(competitors=[])
            result, fetch = _run(ctx)
            self.assertFalse(result.success)
            self.assertIn("analyze_competitors", result.error)
            fetch.assert_not_awaited()
        with self.subTest("fetch that resolves nothing leaves the offer OPEN"):
            # Coverage-based resolution: only an _on_resolved write-back covers
            # a competitor. A run where every competitor failed keeps the offer
            # owed, so the retry happens (cache-served for any that DID land).
            from app.agents.adzump.models import OfferResolution
            from app.agents.adzump.tools.campaign_data import (
                creatives_offer_resolution,
            )
            ctx = _ctx()
            result, _ = _run(ctx)
            self.assertTrue(result.success)
            session_ctx = ctx["session_context"]
            self.assertIs(
                creatives_offer_resolution(session_ctx["campaign_spec"], session_ctx),
                OfferResolution.OPEN)
        with self.subTest("failed fetch also leaves the offer OPEN"):
            ctx = _ctx()
            result, _ = _run(ctx, fetch=mock.AsyncMock(side_effect=RuntimeError("boom")))
            self.assertFalse(result.success)

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
        # every named competitor carries a result -> the offer reads fulfilled
        from app.agents.adzump.models import OfferResolution
        from app.agents.adzump.tools.campaign_data import creatives_offer_resolution
        session_ctx = ctx["session_context"]
        self.assertIs(
            creatives_offer_resolution(session_ctx["campaign_spec"], session_ctx),
            OfferResolution.FULFILLED)


class EssenceRollupTests(unittest.TestCase):
    """The card row's takeaway line, computed from stored verdicts."""

    def test_rollup_rows(self):
        offer = {"essence": {"hookType": "offer"}, "mediaType": "video"}
        aspiration = {"essence": {"hookType": "aspiration"}, "mediaType": "image"}
        other = {"essence": {"hookType": "other"}, "mediaType": "image"}
        bare = {"mediaType": "image"}  # essence never parsed
        for label, ads, items, expected in [
            ("hooks counted, top-2, format split",
             10, [offer, offer, aspiration, other],
             "10 ads · hooks: offer 2, aspiration 1 · 2 video / 2 static"),
            ("no essence at all - just the count", 3, [bare], "3 ads"),
            ("singular ad", 1, [], "1 ad"),
            ("'other' hooks never dominate the story", 2, [other, other],
             "2 ads · 0 video / 2 static"),
        ]:
            with self.subTest(label):
                self.assertEqual(creatives._essence_rollup(ads, items),
                                 expected)


if __name__ == "__main__":
    unittest.main()
