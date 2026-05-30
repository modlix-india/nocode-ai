"""Google Maps API Client Adapter.

Handles raw network communication with Google Maps Platform services (e.g. Geocoding).
"""

from __future__ import annotations

import logging
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Config & Network Constants
MAPS_API_BASE_URL = "https://maps.googleapis.com/maps/api"
DEFAULT_HTTP_TIMEOUT_SECONDS = 10.0


class GoogleMapsClient:
    """Thin adapter over the Google Maps Platform API."""

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or getattr(settings, "GOOGLE_MAPS_API_KEY", None)
        self._timeout = httpx.Timeout(DEFAULT_HTTP_TIMEOUT_SECONDS, connect=3.0)

    @property
    def api_key(self) -> str | None:
        return self._api_key

    async def reverse_geocode(self, lat: float, lng: float) -> list[dict]:
        """Fetch raw reverse-geocoding candidate locations for coordinates."""
        if not self._api_key:
            logger.warning("Google Maps Geocoding failed: API Key is not configured.")
            return []

        url = f"{MAPS_API_BASE_URL}/geocode/json"
        params = {"latlng": f"{lat},{lng}", "key": self._api_key}

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, params=params)
                if response.status_code != 200:
                    logger.warning(
                        "Google Maps Geocoding failed: HTTP status %d. Response: %s",
                        response.status_code,
                        response.text[:200],
                    )
                    return []

                payload = response.json()
                return payload.get("results") or []

        except Exception as e:
            logger.warning(
                "Google Maps Geocoding request failed: %s: %s", type(e).__name__, e
            )
            return []

    async def geocode(self, address: str) -> dict | None:
        """Resolve a physical address string to lat/lng coordinates."""
        if not self._api_key:
            logger.warning("Google Maps Geocoding failed: API Key is not configured.")
            return None

        url = f"{MAPS_API_BASE_URL}/geocode/json"
        params = {"address": address, "key": self._api_key}

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, params=params)
                if response.status_code != 200:
                    logger.warning(
                        "Google Maps Geocoding failed: HTTP status %d. Response: %s",
                        response.status_code,
                        response.text[:200],
                    )
                    return None

                payload = response.json()
                results = payload.get("results") or []
                if not results:
                    return None

                loc = results[0].get("geometry", {}).get("location") or {}
                lat = loc.get("lat")
                lng = loc.get("lng")
                if lat is not None and lng is not None:
                    return {
                        "lat": lat,
                        "lng": lng,
                        "address": results[0].get("formatted_address") or address,
                    }
                return None

        except Exception as e:
            logger.warning(
                "Google Maps Geocoding geocode request failed: %s: %s",
                type(e).__name__,
                e,
            )
            return None


# Module-level singleton client
google_maps_client = GoogleMapsClient()
