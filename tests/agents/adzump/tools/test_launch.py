"""Unit: app/agents/adzump/tools/launch.py — _launch_campaign guards.

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


class LaunchPlatformGateTests(unittest.TestCase):
    def test_cross_platform_id_is_rejected(self):
        ctx = _ctx({"parent_account": "G1", "account": "G2"},
                   account_platforms={"G1": "google", "G2": "google"})
        res = asyncio.run(_launch_campaign({}, ctx))
        self.assertFalse(res.success)
        self.assertIn("different platform", res.error)

    def test_matching_platform_passes_gate(self):
        ctx = _ctx(account_platforms={"M1": "meta", "M2": "meta"})
        with mock.patch("app.agents.adzump.tools.launch.save_campaign",
                        new=mock.AsyncMock(return_value="rec_123")):
            res = asyncio.run(_launch_campaign({}, ctx))
        self.assertTrue(res.success)

    def test_untagged_ids_skip_gate_backcompat(self):
        # Old session with no account_platforms map → no false reject.
        ctx = _ctx()
        with mock.patch("app.agents.adzump.tools.launch.save_campaign",
                        new=mock.AsyncMock(return_value="rec_456")):
            res = asyncio.run(_launch_campaign({}, ctx))
        self.assertTrue(res.success)


class LaunchConsentGateTests(unittest.TestCase):
    def test_no_user_message_blocks_launch(self):
        ctx = _ctx(last_user="")
        res = asyncio.run(_launch_campaign({}, ctx))
        self.assertFalse(res.success)
        self.assertIn("confirmation", res.error)

    def test_non_affirmative_message_blocks_launch(self):
        # e.g. the model jumps the gun while the user asked something else.
        ctx = _ctx(last_user="what budget did we pick?")
        res = asyncio.run(_launch_campaign({}, ctx))
        self.assertFalse(res.success)
        self.assertIn("confirmation", res.error)

    def test_clear_decline_blocks_launch(self):
        ctx = _ctx(last_user="no")
        res = asyncio.run(_launch_campaign({}, ctx))
        self.assertFalse(res.success)

    def test_chip_click_yes_launch_passes(self):
        ctx = _ctx(last_user="Yes, launch")
        with mock.patch("app.agents.adzump.tools.launch.save_campaign",
                        new=mock.AsyncMock(return_value="rec_1")):
            res = asyncio.run(_launch_campaign({}, ctx))
        self.assertTrue(res.success)

    def test_typed_go_ahead_passes(self):
        ctx = _ctx(last_user="go ahead and publish it")
        with mock.patch("app.agents.adzump.tools.launch.save_campaign",
                        new=mock.AsyncMock(return_value="rec_2")):
            res = asyncio.run(_launch_campaign({}, ctx))
        self.assertTrue(res.success)

    def test_helper_word_boundary(self):
        # "yesterday" must not read as "yes"; "eyes" must not match either.
        self.assertFalse(_user_confirmed_launch("yesterday we discussed eyes"))
        self.assertTrue(_user_confirmed_launch("YES"))


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
