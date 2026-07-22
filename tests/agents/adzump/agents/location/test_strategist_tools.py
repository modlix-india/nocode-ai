"""The LocationAgent's two LLM-callable tools - execute contracts.

discover_neighborhoods: coordinates come from session state (never the model);
scan → finalize. geocode_recommendations: the tool schema IS the structured
output - {name, type} picks are geocoded, scale-tagged, finalized. finalize is
patched (covered elsewhere); scans/geocodes are canned.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.agents.location.tools import (
    discover_neighborhoods as dn_mod,
    geocode_recommendations as gr_mod,
)


def _run(coro):
    return asyncio.run(coro)


_ECHO = lambda areas, c, **kw: areas  # finalize stub: return what it was given


class DiscoverNeighborhoodsToolTests(unittest.TestCase):
    def _ctx(self, lat=19.06, lng=72.83):
        place = {}
        if lat is not None:
            place = {"lat": lat, "lng": lng}
        return {"session_context": {"product_data": {"place": place}}}

    def test_scans_from_session_coordinates_and_finalizes(self):
        scanned = [{"name": "Bandra", "pincode": "400050", "lat": 19.05, "lng": 72.83,
                    "distance_km": 1.0, "city": "Mumbai", "state": "MH", "place_id": "p1"}]
        scan = mock.AsyncMock(return_value=scanned)
        fin = mock.AsyncMock(side_effect=_ECHO)
        with mock.patch.object(dn_mod, "scan_neighborhoods", scan), \
             mock.patch.object(dn_mod, "finalize_targets", fin):
            res = _run(dn_mod._discover_neighborhoods({}, self._ctx()))
        self.assertTrue(res.success)
        scan.assert_awaited_once_with(19.06, 72.83, dn_mod.DEFAULT_LOCAL_RADIUS_KM)
        fin.assert_awaited_once()
        self.assertEqual(fin.await_args.args[0], scanned)
        self.assertIn("Bandra", res.summary)

    def test_radius_param_respected(self):
        scan = mock.AsyncMock(return_value=[{"name": "X"}])
        with mock.patch.object(dn_mod, "scan_neighborhoods", scan), \
             mock.patch.object(dn_mod, "finalize_targets", mock.AsyncMock(side_effect=_ECHO)):
            _run(dn_mod._discover_neighborhoods({"radius_km": 3}, self._ctx()))
        scan.assert_awaited_once_with(19.06, 72.83, 3.0)

    def test_no_coordinates_is_a_hard_error_not_a_guess(self):
        fin = mock.AsyncMock()
        with mock.patch.object(dn_mod, "finalize_targets", fin):
            res = _run(dn_mod._discover_neighborhoods({}, self._ctx(lat=None)))
        self.assertFalse(res.success)
        self.assertIn("coordinates", res.error)
        fin.assert_not_awaited()

    def test_empty_scan_is_an_error(self):
        with mock.patch.object(dn_mod, "scan_neighborhoods", mock.AsyncMock(return_value=[])), \
             mock.patch.object(dn_mod, "finalize_targets", mock.AsyncMock()) as fin:
            res = _run(dn_mod._discover_neighborhoods({}, self._ctx()))
        self.assertFalse(res.success)
        fin.assert_not_awaited()


class GeocodeRecommendationsToolTests(unittest.TestCase):
    def _geo(self, name):
        return {"lat": 12.97, "lng": 77.59, "place_id": f"p_{name}",
                "pincode": "", "city": name.split(",")[0], "state": "KA"}

    def test_geocodes_and_scale_tags_the_picks(self):
        geocode = mock.AsyncMock(side_effect=lambda n: self._geo(n))
        fin = mock.AsyncMock(side_effect=_ECHO)
        with mock.patch.object(gr_mod.google_maps_client, "geocode", geocode), \
             mock.patch.object(gr_mod, "finalize_targets", fin):
            res = _run(gr_mod._geocode_recommendations(
                {"locations": [
                    {"name": "Bengaluru, Karnataka, India", "type": "city"},
                    {"name": "Maharashtra, India", "type": "STATE"},   # case-tolerant
                ]},
                {"session_context": {}},
            ))
        self.assertTrue(res.success)
        resolved = fin.await_args.args[0]
        self.assertEqual([a["scale"] for a in resolved], ["city", "state"])
        self.assertEqual(resolved[0]["lat"], 12.97)

    def test_unknown_type_tolerated_as_city_not_dropped(self):
        with mock.patch.object(gr_mod.google_maps_client, "geocode",
                               mock.AsyncMock(side_effect=lambda n: self._geo(n))), \
             mock.patch.object(gr_mod, "finalize_targets", mock.AsyncMock(side_effect=_ECHO)) as fin:
            res = _run(gr_mod._geocode_recommendations(
                {"locations": [{"name": "Goa, India", "type": "metro"}]},
                {"session_context": {}},
            ))
        self.assertTrue(res.success)
        self.assertEqual(fin.await_args.args[0][0]["scale"], "city")

    def test_empty_locations_rejected(self):
        res = _run(gr_mod._geocode_recommendations({"locations": []}, {}))
        self.assertFalse(res.success)

    def test_all_geocode_failures_reported_with_names(self):
        with mock.patch.object(gr_mod.google_maps_client, "geocode",
                               mock.AsyncMock(side_effect=RuntimeError("down"))), \
             mock.patch.object(gr_mod, "finalize_targets", mock.AsyncMock()) as fin:
            res = _run(gr_mod._geocode_recommendations(
                {"locations": [{"name": "Nowhere, XX", "type": "city"}]},
                {"session_context": {}},
            ))
        self.assertFalse(res.success)
        self.assertIn("Nowhere, XX", res.error)
        fin.assert_not_awaited()

    def test_partial_geocode_failure_keeps_the_rest(self):
        async def geocode(name):
            if "Bad" in name:
                raise RuntimeError("down")
            return self._geo(name)
        with mock.patch.object(gr_mod.google_maps_client, "geocode",
                               mock.AsyncMock(side_effect=geocode)), \
             mock.patch.object(gr_mod, "finalize_targets", mock.AsyncMock(side_effect=_ECHO)):
            res = _run(gr_mod._geocode_recommendations(
                {"locations": [{"name": "Bengaluru, India", "type": "city"},
                               {"name": "Bad, India", "type": "city"}]},
                {"session_context": {}},
            ))
        self.assertTrue(res.success)
        self.assertIn("Bad, India", res.summary)   # skipped names surfaced


if __name__ == "__main__":
    unittest.main()
