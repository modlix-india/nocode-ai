"""Unit: app/agents/adzump/tools/launch.py - _launch_campaign guards.

Gate order under test: idempotency → required-fields → platform-mismatch →
user-consent → save. Consent is the harness enforcement of the prompt rule
"never publish without an explicit yes in the user's most recent message";
idempotency stops a double "Yes, launch" / model retry from re-saving.
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from app.agents.adzump.tools.launch import _launch_campaign, _user_confirmed_launch


def _session_with(last_user: str):
    """Minimal session double for _last_user_text (reads .messages)."""
    return SimpleNamespace(messages=[{"role": "user", "content": last_user}])


def _ctx(spec_over=None, *, last_user="Yes, launch", account_platforms=None, session_extra=None):
    spec = {"platform": "Meta", "duration": "30 days", "budget": "₹5,000/day",
            "parent_account": "M1", "account": "M2"}
    spec.update(spec_over or {})
    session_ctx = {"campaign_spec": spec}
    if account_platforms is not None:
        session_ctx["account_platforms"] = account_platforms
    session_ctx.update(session_extra or {})
    return {"session_context": session_ctx, "_session": _session_with(last_user)}


class LaunchGateTests(unittest.TestCase):
    def test_gates_in_order(self):
        # (label, spec override, last user message, account platforms, saved id,
        #  launches, error part)
        meta = {"M1": "meta", "M2": "meta"}
        for label, spec, user, platforms, saved, launches, error in [
            ("a required field missing", {"budget": ""}, "Yes, launch", None, "rec", False,
             "missing required fields: budget"),
            ("an account from another platform", {"parent_account": "G1", "account": "G2"},
             "Yes, launch", {"G1": "google", "G2": "google"}, "rec", False, "different platform"),
            ("no user message", {}, "", None, "rec", False, "confirmation"),
            ("a question, not a go-ahead", {}, "what budget did we pick?", None, "rec", False,
             "confirmation"),
            ("a clear no", {}, "no", None, "rec", False, "confirmation"),
            ("the save failed", {}, "Yes, launch", meta, None, False, "NOT saved"),
            ("matching platform tags", {}, "Yes, launch", meta, "rec", True, ""),
            ("an old session with untagged ids", {}, "Yes, launch", None, "rec", True, ""),
        ]:
            with self.subTest(label):
                ctx = _ctx(spec, last_user=user, account_platforms=platforms)
                with mock.patch("app.agents.adzump.tools.launch.save_campaign",
                                new=mock.AsyncMock(return_value=saved)):
                    res = asyncio.run(_launch_campaign({}, ctx))
                self.assertEqual(res.success, launches)
                if error:
                    self.assertIn(error, res.error)


class LaunchConsentGateTests(unittest.TestCase):
    def test_consent_phrases(self):
        # Only a plain go-ahead passes; a question, negation or hold-off blocks
        # even when it names the action (these all passed before 2026-09-24).
        for message, confirmed in [
            ("YES", True),
            ("Yes, launch", True),
            ("go ahead and publish it", True),
            ("launch it now", True),
            ("yesterday we discussed eyes", False),  # word boundary, not "yes"
            ("don't launch yet", False),
            ("do not publish", False),
            ("hold off on the launch", False),
            ("when will it launch?", False),
            ("can we publish later?", False),
            ("yes, but wait until Monday to launch", False),
        ]:
            with self.subTest(message):
                self.assertEqual(_user_confirmed_launch(message), confirmed)


class LaunchIdempotencyTests(unittest.TestCase):
    def test_second_launch_short_circuits_without_saving(self):
        ctx = _ctx(session_extra={"product_id": "rec_prev"},
                   spec_over={"campaign_status": "launched"})
        save = mock.AsyncMock(return_value="rec_should_not_happen")
        with mock.patch("app.agents.adzump.tools.launch.save_campaign", new=save):
            res = asyncio.run(_launch_campaign({}, ctx))
        self.assertTrue(res.success)
        self.assertEqual(res.data["product_id"], "rec_prev")
        self.assertIn("already", res.summary.lower())
        save.assert_not_awaited()

    def test_launched_flag_without_product_id_does_not_short_circuit(self):
        # Half-written state (flag but no id) falls through to the normal path.
        ctx = _ctx(spec_over={"campaign_status": "launched"})
        with mock.patch("app.agents.adzump.tools.launch.save_campaign",
                        new=mock.AsyncMock(return_value="rec_9")):
            res = asyncio.run(_launch_campaign({}, ctx))
        self.assertTrue(res.success)
        self.assertEqual(res.data["product_id"], "rec_9")


if __name__ == "__main__":
    unittest.main()
