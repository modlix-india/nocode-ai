"""LocationAgent.add / .delete — the deterministic (no-LLM) actions.

Both mutate product.target_areas and end in finalize_targets (map → persist →
re-render). finalize is patched here — its own behavior is covered by
test_strategist_tools/test_platform_mapping — so these tests lock the agent's
mutation + validation + routing contract.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.agents.location import agent as agent_mod
from app.agents.adzump.agents.location.agent import get_location_agent


def _ctx(target_areas=None):
    return {
        "session_context": {
            "product_data": {"target_areas": list(target_areas or [])},
            "campaign_spec": {"platform": "Meta"},
        }
    }


def _run(coro):
    return asyncio.run(coro)


class AddTests(unittest.TestCase):
    def test_add_appends_and_finalizes(self):
        ctx = _ctx()
        fin = mock.AsyncMock(side_effect=lambda areas, c: areas)
        with mock.patch.object(agent_mod, "finalize_targets", fin), \
             mock.patch("app.services.llm_provider.get_llm_provider",
                        side_effect=AssertionError("add must not wake the LLM")):
            res = _run(get_location_agent().add(
                {"action": "add", "name": "Juhu", "lat": 19.1, "lng": 72.83,
                 "pincode": "400049", "radius": 3, "meta_key": "555", "meta_type": "zip"},
                ctx,
            ))
        self.assertTrue(res.success)
        fin.assert_awaited_once()
        areas = ctx["session_context"]["product_data"]["target_areas"]
        self.assertEqual(len(areas), 1)
        area = areas[0]
        self.assertEqual(area["name"], "Juhu")
        self.assertEqual(area["distance_km"], 3)          # radius → distance_km
        self.assertEqual(area["meta_key"], "555")         # widget wire fields preserved
        self.assertEqual(area["meta_type"], "zip")

    def test_add_without_name_rejected_before_side_effects(self):
        ctx = _ctx()
        fin = mock.AsyncMock()
        with mock.patch.object(agent_mod, "finalize_targets", fin):
            res = _run(get_location_agent().add({"action": "add"}, ctx))
        self.assertFalse(res.success)
        fin.assert_not_awaited()

    def test_missing_session_context_rejected(self):
        res = _run(get_location_agent().add({"name": "X"}, {}))
        self.assertFalse(res.success)


class DeleteTests(unittest.TestCase):
    def test_delete_pops_one_based_index(self):
        ctx = _ctx([{"name": "A"}, {"name": "B"}, {"name": "C"}])
        fin = mock.AsyncMock(side_effect=lambda areas, c: areas)
        with mock.patch.object(agent_mod, "finalize_targets", fin):
            res = _run(get_location_agent().delete({"action": "delete", "index": 2}, ctx))
        self.assertTrue(res.success)
        names = [a["name"] for a in ctx["session_context"]["product_data"]["target_areas"]]
        self.assertEqual(names, ["A", "C"])

    def test_delete_invalid_index_rejected_before_side_effects(self):
        ctx = _ctx([{"name": "A"}])
        fin = mock.AsyncMock()
        with mock.patch.object(agent_mod, "finalize_targets", fin):
            for bad in (None, 0, 2):
                res = _run(get_location_agent().delete({"index": bad}, ctx))
                self.assertFalse(res.success)
        fin.assert_not_awaited()
        self.assertEqual(len(ctx["session_context"]["product_data"]["target_areas"]), 1)


if __name__ == "__main__":
    unittest.main()
