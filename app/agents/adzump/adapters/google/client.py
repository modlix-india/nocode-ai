"""Google Ads REST client.

Ported from ``ds/adapters/google/client.py``. Key adaptations:
- Reads the developer token from ``get_adzump_config().google_ads`` (not ``os.getenv``).
- Auth headers (the user's forwarded request headers) are passed per-request
  rather than read from a global ContextVar.
- Raises plain ``RuntimeError`` / ``httpx.HTTPStatusError`` instead of ds's custom
  exception classes.
- Uses ``httpx.AsyncClient`` directly (no dependency on ds's http_request wrapper).
"""

from __future__ import annotations

import logging
import time

import httpx

from app.agents.adzump.adapters.connections import fetch_google_api_token
from app.agents.adzump.config import get_adzump_config

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

_cached_oauth_token: str | None = None
_oauth_token_expiry: float = 0.0


class GoogleAdsClient:
    """Thin wrapper over the Google Ads REST API (v23)."""

    BASE_URL = "https://googleads.googleapis.com"
    API_VERSION = "v23"

    def __init__(self) -> None:
        self._timeout = httpx.Timeout(30.0, connect=10.0)

    @property
    def developer_token(self) -> str:
        token = get_adzump_config().google_ads.developer_token
        if not token:
            raise RuntimeError(
                "GOOGLE_ADS_DEVELOPER_TOKEN not configured. "
                "Set ai.adzump.googleAds.developerToken in nocode-saas config, "
                "or ADZUMP_GOOGLE_ADS_DEVELOPER_TOKEN env var for local dev."
            )
        return token

    async def get(
        self, endpoint: str, client_code: str, auth_headers: dict[str, str],
    ) -> dict:
        token = await self._get_api_token(client_code, auth_headers)
        url = f"{self.BASE_URL}/{self.API_VERSION}/{endpoint}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(url, headers=self._build_auth_headers(token))
            _raise_for_google_error(response)
            return response.json()

    async def search(
        self,
        query: str,
        customer_id: str,
        login_customer_id: str,
        client_code: str,
        auth_headers: dict[str, str],
    ) -> list:
        """Execute a GAQL query via googleAds:search."""
        token = await self._get_api_token(client_code, auth_headers)
        url = f"{self.BASE_URL}/{self.API_VERSION}/customers/{customer_id}/googleAds:search"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                url,
                headers=self._build_auth_headers(token, login_customer_id),
                json={"query": query},
            )
            _raise_for_google_error(response)
            return response.json().get("results", [])

    async def search_stream(
        self,
        query: str,
        customer_id: str,
        login_customer_id: str,
        client_code: str,
        auth_headers: dict[str, str],
    ) -> list:
        token = await self._get_api_token(client_code, auth_headers)
        url = f"{self.BASE_URL}/{self.API_VERSION}/customers/{customer_id}/googleAds:searchStream"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                url,
                headers=self._build_auth_headers(token, login_customer_id),
                json={"query": query},
            )
            _raise_for_google_error(response)
            return self._parse_stream(response.json())

    async def _get_api_token(
        self, client_code: str, auth_headers: dict[str, str],
    ) -> str:
        # Priority order:
        # 1. Direct access token from config (short-lived, pasted by dev)
        # 2. Local OAuth refresh flow (refresh_token + client creds)
        # 3. nocode-saas connection service (prod / per-user)
        creds = get_adzump_config().google_ads
        if creds.access_token:
            return creds.access_token
        local = await _refresh_local_oauth_token()
        if local:
            return local
        return await fetch_google_api_token(client_code, auth_headers)

    def _build_auth_headers(
        self, access_token: str, login_customer_id: str | None = None,
    ) -> dict[str, str]:
        if not access_token:
            raise RuntimeError(
                "No Google OAuth access token available. "
                "Either (a) connect a Google account in the nocode-saas UI so the "
                "connection service can return a token, or (b) for local dev, set "
                "ai.adzump.googleAds.refreshToken/clientId/clientSecret in "
                "application-default.yml (or the ADZUMP_GOOGLE_ADS_REFRESH_TOKEN/"
                "CLIENT_ID/CLIENT_SECRET env vars)."
            )
        headers = {
            "Authorization": f"Bearer {access_token}",
            "developer-token": self.developer_token,
            "Content-Type": "application/json",
        }
        if login_customer_id:
            headers["login-customer-id"] = login_customer_id
        return headers

    @staticmethod
    def _parse_stream(response_json) -> list:
        if isinstance(response_json, list):
            results: list = []
            for batch in response_json:
                results.extend(batch.get("results", []))
            return results
        return response_json.get("results", [])


async def _refresh_local_oauth_token() -> str | None:
    """Use GOOGLE_ADS_REFRESH_TOKEN + client creds to get an access token.

    Skipped unless all three local-dev credentials are configured. Cached for
    ~1h to avoid a token refresh on every request.
    """
    global _cached_oauth_token, _oauth_token_expiry

    creds = get_adzump_config().google_ads
    if not (creds.refresh_token and creds.client_id and creds.client_secret):
        return None

    if _cached_oauth_token and time.time() < _oauth_token_expiry:
        return _cached_oauth_token

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": creds.refresh_token,
                    "client_id": creds.client_id,
                    "client_secret": creds.client_secret,
                },
            )
            resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.error("google_oauth_refresh_failed: %s", str(e)[:200])
        raise RuntimeError(
            "Google OAuth refresh failed. Check ADZUMP_GOOGLE_ADS_* env vars.",
        ) from e

    data = resp.json()
    _cached_oauth_token = data["access_token"]
    _oauth_token_expiry = time.time() + data.get("expires_in", 3600) - 60
    logger.info("google_oauth_token_refreshed")
    return _cached_oauth_token


def _raise_for_google_error(response: httpx.Response) -> None:
    if response.status_code < 400:
        return

    message = f"Google Ads API {response.status_code}"
    try:
        error = response.json().get("error", {})
        if error.get("message"):
            message = f"Google Ads API {response.status_code}: {error['message']}"
    except Exception:
        pass

    logger.warning(
        "google_ads_error: status=%d body=%s",
        response.status_code, response.text[:400],
    )
    raise RuntimeError(message)


# Singleton instance
google_ads_client = GoogleAdsClient()
