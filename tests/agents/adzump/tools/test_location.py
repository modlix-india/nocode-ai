"""location._detected_location - the place.address accessor (wire shapes are
normalized into place at the merge boundary, tools/product.py) - and
confirm_location's one server-side geocode (pin coords + country code).

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump.tools.test_location -v
"""
from __future__ import annotations

import unittest
from unittest import mock

from app.agents.adzump.tools.location import _confirm_location, _detected_location


class DetectedLocationTests(unittest.TestCase):
    def test_table(self):
        for product, expected in [
            ({"place": {"address": "Bengaluru"}}, "Bengaluru"),
            ({"place": {"address": "  Pune  "}}, "Pune"),
            ({"place": {}}, ""),
            ({"place": None}, ""),
            ({}, ""),
        ]:
            with self.subTest(product=product):
                self.assertEqual(_detected_location(product), expected)


class _Stream:
    def __init__(self):
        self.data = []

    async def emit_text(self, text):
        pass

    async def emit_data(self, name, payload):
        self.data.append((name, payload))


class ConfirmLocationTests(unittest.IsolatedAsyncioTestCase):
    """The map pins what the backend geocoded (so an untouched pin keeps the
    detected address) and the country code lands before the ad search needs it."""

    async def _run(self, geo):
        session_ctx = {"product_data": {
            "business_type": "Residential real estate", "product_name": "Misty Shores",
            "place": {"address": "Near ITPB (Whitefield), Bangalore"}}}
        stream = _Stream()
        maps = mock.MagicMock()
        maps.return_value.geocode = mock.AsyncMock(return_value=geo)
        with mock.patch("app.agents.adzump.adapters.google.maps.GoogleMapsClient", maps):
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

    async def test_geocode_miss_leaves_the_map_to_geocode(self):
        ctx, payload = await self._run(None)
        self.assertNotIn("coordinates", payload)
        self.assertEqual(ctx["product_data"]["place"].get("country_code", ""), "")
        self.assertIsNone(ctx["_pending_location_confirm"]["lat"])


if __name__ == "__main__":
    unittest.main()
