"""tools/location.py: confirm_location's one server-side geocode (pin coords +
country code), and manage_targeting_locations' empty-message guard.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump.tools.test_location -v
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.tools.location import _confirm_location, manage_targeting_locations
from tests.agents.adzump._fixtures import FakeStream


class ConfirmLocationTests(unittest.IsolatedAsyncioTestCase):
    """The map pins what the backend geocoded (so an untouched pin keeps the
    detected address) and the country code lands before the ad search needs it."""

    async def _run(self, geo):
        session_ctx = {"product_data": {
            "business_type": "Residential real estate", "product_name": "Misty Shores",
            "place": {"address": "Near ITPB (Whitefield), Bangalore"}}}
        stream = FakeStream()
        maps = mock.MagicMock()
        maps.return_value.geocode = mock.AsyncMock(return_value=geo)
        with mock.patch("app.agents.adzump.adapters.google.maps.GoogleMapsClient", maps), \
             mock.patch("app.agents.adzump.tools.location.save_place",
                        new=mock.AsyncMock()) as self.m_save:
            result = await _confirm_location(
                {}, {"session_context": session_ctx, "event_stream": stream})
        self.assertTrue(result.success)
        return session_ctx, stream.data[0][1]

    async def test_geocoded(self):
        ctx, payload = await self._run(
            {"lat": 12.97, "lng": 77.73, "country_code": "IN", "address": "x"})
        self.assertEqual(payload["coordinates"], {"lat": 12.97, "lng": 77.73})
        self.assertEqual(ctx["product_data"]["place"]["country_code"], "IN")
        self.assertEqual(ctx["_pending_location_confirm"],
                         {"address": "Near ITPB (Whitefield), Bangalore",
                          "lat": 12.97, "lng": 77.73})
        self.assertNotIn("lat", ctx["product_data"]["place"])  # confirm not implied
        self.m_save.assert_awaited_once()  # the country code is saved on the product

    async def test_geocode_miss_leaves_the_map_to_geocode(self):
        ctx, payload = await self._run(None)
        self.assertNotIn("coordinates", payload)
        self.assertEqual(ctx["product_data"]["place"].get("country_code", ""), "")
        self.m_save.assert_not_awaited()
        self.assertIsNone(ctx["_pending_location_confirm"]["lat"])


class ManageTargetingGuardTests(unittest.TestCase):
    """The orchestrator-side wrapper is the ONE owner of the empty-message
    guard - its retry-hint error is what the orchestrator relays; the location
    agent's handle() assumes a non-empty message."""

    def test_empty_user_message_rejected_with_retry_hint(self):
        for params in ({}, {"user_message": ""}, {"user_message": "   "}):
            with self.subTest(params=params):
                res = asyncio.run(manage_targeting_locations.execute(params, {}))
                self.assertFalse(res.success)
                self.assertIn("verbatim", res.error)


if __name__ == "__main__":
    unittest.main()
