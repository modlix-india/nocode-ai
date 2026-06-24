"""Unit: app/agents/adzump/tools/launch.py — _launch_campaign cross-platform id gate."""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.tools.launch import _launch_campaign


class LaunchPlatformGateTests(unittest.TestCase):
    def _full(self, **over):
        spec = {"platform": "Meta", "duration": "30 days", "budget": "₹5,000/day",
                "parent_account": "G1", "account": "G2"}
        spec.update(over)
        return {"session_context": {"campaign_spec": spec,
                                    "account_platforms": {"G1": "google", "G2": "google"}}}

    def test_cross_platform_id_is_rejected(self):
        res = asyncio.run(_launch_campaign({}, self._full()))
        self.assertFalse(res.success)
        self.assertIn("different platform", res.error)

    def test_matching_platform_passes_gate(self):
        ctx = {"session_context": {
            "campaign_spec": {"platform": "Meta", "duration": "30 days",
                              "budget": "₹5,000/day", "parent_account": "M1", "account": "M2"},
            "account_platforms": {"M1": "meta", "M2": "meta"}}}
        with mock.patch("app.agents.adzump.tools.launch.save_campaign",
                        new=mock.AsyncMock(return_value="rec_123")):
            res = asyncio.run(_launch_campaign({}, ctx))
        self.assertTrue(res.success)                                 # gate let it through

    def test_untagged_ids_skip_gate_backcompat(self):
        # Old session with no account_platforms map → no false reject.
        ctx = {"session_context": {
            "campaign_spec": {"platform": "Meta", "duration": "30 days",
                              "budget": "₹5,000/day", "parent_account": "X1", "account": "X2"}}}
        with mock.patch("app.agents.adzump.tools.launch.save_campaign",
                        new=mock.AsyncMock(return_value="rec_456")):
            res = asyncio.run(_launch_campaign({}, ctx))
        self.assertTrue(res.success)


if __name__ == "__main__":
    unittest.main()
