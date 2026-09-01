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
PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
# Bias circle for business-profile lookups: wide enough for a metro area,
# narrow enough to break same-name-other-city ties. Places caps it at 50km.
PLACES_BIAS_RADIUS_METERS = 50000.0
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

    async def find_business_website(
        self, name: str, *, lat: float | None = None, lng: float | None = None
    ) -> dict | None:
        """Google Business Profile lookup via Places Text Search (New).

        Returns ``{"name", "website"}`` for the top listing matching ``name``,
        biased to the given point when provided, or None when there is no
        listing, the listing has no website, or the API is unavailable. The
        caller owns acceptance (name similarity, host quality) - this is a
        dumb lookup."""
        if not self.api_key or not name.strip():
            return None
        body: dict = {"textQuery": name, "pageSize": 1}
        if lat is not None and lng is not None:
            body["locationBias"] = {"circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": PLACES_BIAS_RADIUS_METERS,
            }}
        headers = {
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": "places.displayName,places.websiteUri",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    PLACES_SEARCH_URL, headers=headers, json=body)
        except Exception as e:
            logger.warning("places_search failed for %r: %s: %s",
                           name, type(e).__name__, e)
            return None
        if response.status_code != 200:
            logger.warning("places_search non-200 for %r: %d %s",
                           name, response.status_code, response.text[:200])
            return None
        places = (response.json() or {}).get("places") or []
        if not places:
            return None
        website = (places[0].get("websiteUri") or "").strip()
        if not website:
            return None
        listing_name = ((places[0].get("displayName") or {}).get("text") or "").strip()
        return {"name": listing_name, "website": website}

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
                country = None
                country_code = None  # unknown stays unknown, never assumed

                for comp in components:
                    types = comp.get("types", [])
                    if "postal_code" in types:
                        pincode = comp.get("long_name", "").strip()
                    elif "locality" in types:
                        city = comp.get("long_name", "").strip()
                    elif "administrative_area_level_1" in types:
                        state = comp.get("short_name", "").strip()
                    elif "country" in types:
                        country = comp.get("long_name", "").strip()
                        country_code = comp.get("short_name", "").strip()

                return {
                    "lat": lat,
                    "lng": lng,
                    "address": results[0].get("formatted_address") or address,
                    "place_id": results[0].get("place_id"),
                    "pincode": pincode,
                    "city": city,
                    "state": state,
                    "country": country,
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
