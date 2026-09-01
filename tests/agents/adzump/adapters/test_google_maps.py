"""GoogleMapsClient.find_business_website - the Places business-profile lookup."""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.adapters.google import maps as maps_module
from app.agents.adzump.adapters.google.maps import GoogleMapsClient


def _client_returning(status_code: int, payload: dict):
    """Stub httpx.AsyncClient whose post() yields the given response."""
    response = mock.Mock(status_code=status_code, text="")
    response.json.return_value = payload
    stub = mock.Mock()
    stub.post = mock.AsyncMock(return_value=response)
    ctx = mock.MagicMock()
    ctx.__aenter__ = mock.AsyncMock(return_value=stub)
    ctx.__aexit__ = mock.AsyncMock(return_value=False)
    return ctx, stub


class FindBusinessWebsiteTests(unittest.TestCase):
    def _lookup(self, status_code=200, payload=None, api_key="k", **kwargs):
        ctx, self.http = _client_returning(status_code, payload or {})
        with mock.patch.object(maps_module.httpx, "AsyncClient", return_value=ctx), \
             mock.patch.object(maps_module.settings, "GOOGLE_MAPS_API_KEY", api_key):
            return asyncio.run(
                GoogleMapsClient().find_business_website("Lodha Azur", **kwargs))

    def test_returns_listing_name_and_website(self):
        result = self._lookup(payload={"places": [{
            "displayName": {"text": "Lodha Azur"},
            "websiteUri": "https://lodhagroup.com/azur",
        }]})
        self.assertEqual(result, {"name": "Lodha Azur",
                                  "website": "https://lodhagroup.com/azur"})

    def test_locality_bias_sent_when_coords_given(self):
        self._lookup(payload={"places": []}, lat=12.9, lng=77.6)
        body = self.http.post.await_args.kwargs["json"]
        center = body["locationBias"]["circle"]["center"]
        self.assertEqual((center["latitude"], center["longitude"]), (12.9, 77.6))

    def test_none_rows(self):
        rows = [
            ("no api key", {"api_key": ""}),
            ("non-200", {"status_code": 403}),
            ("no listings", {"payload": {"places": []}}),
            ("listing without website", {"payload": {"places": [
                {"displayName": {"text": "Lodha Azur"}}]}}),
        ]
        for label, overrides in rows:
            with self.subTest(label):
                self.assertIsNone(self._lookup(**overrides))


if __name__ == "__main__":
    unittest.main()
