"""Meta Graph API client.

Ported from ``ds/adapters/meta/client.py``. Reads ``META_ACCESS_TOKEN`` from the
agent-level config as a local-dev override; falls through to the nocode-saas
connection service for prod/per-user tokens.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.agents.adzump.adapters.connections import fetch_meta_api_token
from app.agents.adzump.config import get_adzump_config

logger = logging.getLogger(__name__)

META_BASE_URL = "https://graph.facebook.com/v22.0"


class MetaClient:
    BASE_URL = META_BASE_URL

    def __init__(self) -> None:
        self._timeout = httpx.Timeout(30.0, connect=10.0)

    async def get(
        self,
        endpoint: str,
        client_code: str,
        auth_headers: dict[str, str],
        params: dict[str, Any] | None = None,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        token = access_token or await self._get_api_token(client_code, auth_headers)
        url = f"{self.BASE_URL}{endpoint}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(url, headers=self._build_headers(token), params=params)
            _raise_for_meta_error(response)
            return response.json()

    async def post(
        self,
        endpoint: str,
        client_code: str,
        auth_headers: dict[str, str],
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        token = access_token or await self._get_api_token(client_code, auth_headers)
        url = f"{self.BASE_URL}{endpoint}"
        headers = {"Authorization": f"Bearer {token}"}
        if json is not None:
            headers["Content-Type"] = "application/json"

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                url, headers=headers, json=json, data=data, files=files, params=params,
            )
            _raise_for_meta_error(response)
            return response.json()

    async def _get_api_token(
        self, client_code: str, auth_headers: dict[str, str],
    ) -> str:
        local = get_adzump_config().meta.access_token
        if local:
            return local
        return await fetch_meta_api_token(client_code, auth_headers)

    @staticmethod
    def _build_headers(access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }


def _raise_for_meta_error(response: httpx.Response) -> None:
    if response.status_code < 400:
        return

    message = f"Meta Graph API {response.status_code}"
    try:
        error = response.json().get("error", {})
        if error.get("message"):
            message = f"Meta Graph API {response.status_code}: {error['message']}"
    except Exception:
        pass

    logger.warning("meta_api_error: status=%d body=%s",
                   response.status_code, response.text[:400])
    raise RuntimeError(message)


# Singleton instance
meta_client = MetaClient()
