"""add_location / delete_location - the deterministic edit tools.

Both mutate product.target_areas and end in finalize_targets (map → persist →
re-render). finalize is patched here - its own behavior is covered by
test_strategist_tools/test_platform_mapping - so these tests lock the tools'
mutation + validation contract, and that their schemas mirror the params
models (models.py is the single source of truth).
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.agents.location.tools import edit_locations as edit_mod
from app.agents.adzump.agents.location.tools.edit_locations import (
    add_location_tool,
    delete_location_tool,
)


def _ctx(target_areas=None):
    return {
        "session_context": {
            "product_data": {"target_areas": list(target_areas or [])},
            "campaign_spec": {"platform": "Meta"},
        }
    }


def _run(coro):
    return asyncio.run(coro)


def _fake_finalize():
    """Emulate the real funnel's commit: it assigns product["target_areas"]
    itself (see _shared.finalize_targets) - the edit tools pass a NEW list
    and never touch the live one."""
    async def _fin(areas, context):
        context["session_context"]["product_data"]["target_areas"] = areas
        return areas
    return mock.AsyncMock(side_effect=_fin)


class AddLocationTests(unittest.TestCase):
    def test_add_appends_and_finalizes(self):
        ctx = _ctx()
        fin = _fake_finalize()
        with mock.patch.object(edit_mod, "finalize_targets", fin):
            res = _run(add_location_tool.execute(
                {"name": "Juhu", "lat": 19.1, "lng": 72.83,
                 "pincode": "400049", "radius": 3, "scale": "city",
                 # hallucination guard (PR #91 B2): platform handles are NOT in
                 # the schema - if the model invents them anyway they must be
                 # dropped at the boundary, never stored past the mapper lookup.
                 "key": "555", "resourceName": "geoTargetConstants/999", "type": "zip"},
                ctx,
            ))
        self.assertTrue(res.success)
        fin.assert_awaited_once()
        areas = ctx["session_context"]["product_data"]["target_areas"]
        self.assertEqual(len(areas), 1)
        area = areas[0]
        self.assertEqual(area["name"], "Juhu")
        self.assertEqual(area["distance_km"], 3)          # radius → distance_km
        self.assertEqual(area["scale"], "city")           # guards pincode backfill scope
        for handle in ("key", "resourceName", "type", "place_id",
                       "google_name", "meta_name"):
            self.assertNotIn(handle, area)
        # the receipt names the area so the agent's summary stays grounded
        self.assertIn("Juhu", res.summary)

    def test_add_invalid_params_rejected_before_side_effects(self):
        """LLM → Python boundary: no/empty name fails the pydantic parse and
        returns a structured error WITHOUT mutating or finalizing."""
        fin = mock.AsyncMock()
        for bad_params in ({}, {"name": ""}, {"city": "Mumbai"}):
            with self.subTest(params=bad_params):
                ctx = _ctx()
                with mock.patch.object(edit_mod, "finalize_targets", fin):
                    res = _run(add_location_tool.execute(bad_params, ctx))
                self.assertFalse(res.success)
                self.assertIn("Invalid params", res.error)
                self.assertEqual(
                    ctx["session_context"]["product_data"]["target_areas"], [])
        fin.assert_not_awaited()

    def test_missing_session_context_rejected(self):
        res = _run(add_location_tool.execute({"name": "X"}, {}))
        self.assertFalse(res.success)

    def test_finalize_failure_leaves_memory_untouched(self):
        """PR #91 J1: the tool passes a NEW list; if the funnel blows up,
        the live target_areas must not carry an unpersisted area."""
        ctx = _ctx([{"name": "A"}])
        fin = mock.AsyncMock(side_effect=RuntimeError("save exploded"))
        with mock.patch.object(edit_mod, "finalize_targets", fin):
            with self.assertRaises(RuntimeError):
                _run(add_location_tool.execute({"name": "Juhu"}, ctx))
        names = [a["name"] for a in ctx["session_context"]["product_data"]["target_areas"]]
        self.assertEqual(names, ["A"])


class DeleteLocationTests(unittest.TestCase):
    def test_delete_pops_one_based_index(self):
        ctx = _ctx([{"name": "A"}, {"name": "B"}, {"name": "C"}])
        fin = _fake_finalize()
        with mock.patch.object(edit_mod, "finalize_targets", fin):
            res = _run(delete_location_tool.execute({"index": 2}, ctx))
        self.assertTrue(res.success)
        names = [a["name"] for a in ctx["session_context"]["product_data"]["target_areas"]]
        self.assertEqual(names, ["A", "C"])
        # The receipt must name the AREA that was removed, not a survivor.
        self.assertIn("B", res.summary)

    def test_delete_invalid_index_rejected_before_side_effects(self):
        """index >= 1 lives on the model; the upper bound needs the live list.
        Both invalid shapes must reject without mutating or finalizing."""
        fin = mock.AsyncMock()
        for bad_params in ({}, {"index": 0}, {"index": 2}):
            with self.subTest(params=bad_params):
                ctx = _ctx([{"name": "A"}])
                with mock.patch.object(edit_mod, "finalize_targets", fin):
                    res = _run(delete_location_tool.execute(bad_params, ctx))
                self.assertFalse(res.success)
                self.assertEqual(
                    len(ctx["session_context"]["product_data"]["target_areas"]), 1)
        fin.assert_not_awaited()


class ToolSchemaTests(unittest.TestCase):
    """The tool schemas are GENERATED from the params models - lock the facts
    that come from models.py, not from hand-copies."""

    def test_add_schema_mirrors_model(self):
        params = {p.name: p for p in add_location_tool.parameters}
        self.assertEqual(
            [name for name, p in params.items() if p.required], ["name"])
        # Optional[float] must flatten anyOf[number, null] → "number"
        self.assertEqual(params["lat"].type, "number")

    def test_delete_schema_mirrors_model(self):
        params = {p.name: p for p in delete_location_tool.parameters}
        self.assertEqual(set(params), {"index"})
        self.assertEqual(params["index"].type, "integer")
        self.assertTrue(params["index"].required)


if __name__ == "__main__":
    unittest.main()
