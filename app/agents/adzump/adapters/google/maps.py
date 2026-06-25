"""Google Maps Platform adapter.

Handles geocoding and reverse geocoding requests with built-in retries
and structured response parsing.
"""

from __future__ import annotations

import asyncio
import logging
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

MAPS_API_BASE_URL = "https://maps.googleapis.com/maps/api"
DEFAULT_HTTP_TIMEOUT_SECONDS = 10.0
MAX_RETRIES = 3


class GoogleMapsClient:
    """HTTP client wrapper for Google Maps Geocoding services."""

    def __init__(self) -> None:
        self._timeout = httpx.Timeout(DEFAULT_HTTP_TIMEOUT_SECONDS)

    @property
    def api_key(self) -> str:
        return settings.GOOGLE_MAPS_API_KEY

    async def _request_with_retry(
        self, url: str, params: dict
    ) -> httpx.Response | None:
        """GET request execution with exponential backoff retry strategy."""
        if not self.api_key:
            logger.error("Google Maps API key is not configured.")
            return None

        for attempt in range(MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.get(url, params=params)
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt < MAX_RETRIES - 1:
                            delay = 2**attempt
                            logger.warning(
                                "Google Maps API retry %d/%d after %ds on status %d",
                                attempt + 1,
                                MAX_RETRIES,
                                delay,
                                response.status_code,
                            )
                            await asyncio.sleep(delay)
                            continue
                    return response
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    delay = 2**attempt
                    logger.warning(
                        "Google Maps API retry %d/%d after %ds on error: %s",
                        attempt + 1,
                        MAX_RETRIES,
                        delay,
                        e,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.warning(
                    "Google Maps API request failed after %d retries: %s",
                    MAX_RETRIES,
                    e,
                )
                return None
        return None

    async def reverse_geocode(self, lat: float, lng: float) -> list[dict]:
        """Fetch reverse-geocoding candidate locations for coordinates."""
        url = f"{MAPS_API_BASE_URL}/geocode/json"
        params = {"latlng": f"{lat},{lng}", "key": self.api_key}

        response = await self._request_with_retry(url, params)
        if response is None or response.status_code != 200:
            logger.warning(
                "Google Maps Reverse Geocoding failed: status=%s",
                response.status_code if response else "No response"
            )
            return []

        try:
            payload = response.json()
            return payload.get("results") or []
        except Exception as e:
            logger.warning(
                "Google Maps Reverse Geocoding response parse failed: %s: %s",
                type(e).__name__,
                e,
            )
            return []

    async def geocode(self, address: str) -> dict | None:
        """Geocode an address string to fetch coordinates and parsed components."""
        url = f"{MAPS_API_BASE_URL}/geocode/json"
        params = {"address": address, "key": self.api_key}

        response = await self._request_with_retry(url, params)
        if response is None or response.status_code != 200:
            logger.warning(
                "Google Maps Geocoding failed: status=%s",
                response.status_code if response else "No response"
            )
            return None

        try:
            payload = response.json()
            results = payload.get("results") or []
            if not results:
                return None

            geometry = results[0].get("geometry", {})
            loc = geometry.get("location") or {}
            lat = loc.get("lat")
            lng = loc.get("lng")

            if lat is not None and lng is not None:
                components = results[0].get("address_components") or []
                pincode = None
                city = None
                state = None
                country_code = "IN"

                for comp in components:
                    types = comp.get("types", [])
                    if "postal_code" in types:
                        pincode = comp.get("long_name", "").strip()
                    elif "locality" in types:
                        city = comp.get("long_name", "").strip()
                    elif "administrative_area_level_1" in types:
                        state = comp.get("short_name", "").strip()
                    elif "country" in types:
                        country_code = comp.get("short_name", "").strip()

                return {
                    "lat": lat,
                    "lng": lng,
                    "address": results[0].get("formatted_address") or address,
                    "place_id": results[0].get("place_id"),
                    "pincode": pincode,
                    "city": city,
                    "state": state,
                    "country_code": country_code,
                }
            return None

        except Exception as e:
            logger.warning(
                "Google Maps Geocoding response parse failed: %s: %s",
                type(e).__name__,
                e,
            )
            return None


# Singleton instance
google_maps_client = GoogleMapsClient()
