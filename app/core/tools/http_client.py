"""Async HTTP client for calling nocode-saas Gateway APIs.

All agent tools route their API calls through this client.
Auth headers (Authorization, clientCode, appCode) are passed per-call
from the session context - the client itself is stateless.

Usage:
    client = SaasClient("http://localhost:8080")

    result = await client.get("/api/ui/pages", headers=auth_headers, params={"appCode": "myapp"})
    if result.success:
        pages = result.data
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.tools.base import ToolResult

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0  # seconds

_SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie"}


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of headers with sensitive values redacted for logging."""
    return {
        k: ("***" if k.lower() in _SENSITIVE_HEADERS else v)
        for k, v in headers.items()
    }


class SaasClient:
    """Async HTTP client for the nocode-saas Gateway.

    Creates a shared httpx.AsyncClient on first use and reuses it
    for connection pooling. Must call close() on shutdown.
    """

    def __init__(self, gateway_url: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.gateway_url,
                timeout=httpx.Timeout(self.timeout),
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ── HTTP methods ────────────────────────────────────────────

    async def get(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> ToolResult:
        """GET request to the gateway."""
        return await self._request("GET", path, headers=headers, params=params)

    async def post(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> ToolResult:
        """POST request to the gateway."""
        return await self._request("POST", path, headers=headers, json=json, params=params)

    async def put(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> ToolResult:
        """PUT request to the gateway."""
        return await self._request("PUT", path, headers=headers, json=json, params=params)

    async def patch(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> ToolResult:
        """PATCH request to the gateway."""
        return await self._request("PATCH", path, headers=headers, json=json, params=params)

    async def delete(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> ToolResult:
        """DELETE request to the gateway."""
        return await self._request("DELETE", path, headers=headers, params=params)

    # ── Internal ────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Execute an HTTP request and return a structured ToolResult."""
        client = self._get_client()
        url = path if path.startswith("/") else f"/{path}"

        # Standalone mode: extract path prefix from headers and prepend to URL.
        # The X-Path-Prefix header is set by the webpack proxy and carried in
        # the tool context headers - it is stripped before forwarding to the backend.
        if headers and "X-Path-Prefix" in headers:
            url = headers.pop("X-Path-Prefix") + url

        logger.info(f"→ {method} {self.gateway_url}{url}")

        try:
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                json=json,
                params=params,
            )

            logger.info(f"← {method} {url} → {response.status_code}")

            if response.status_code >= 400:
                error_result = self._error_result(response)
                logger.warning(f"  ERROR: {error_result.error}")
                return error_result

            # Parse response body
            data = None
            if response.content:
                content_type = response.headers.get("content-type", "")
                if "application/json" in content_type:
                    data = response.json()
                else:
                    data = response.text

            return ToolResult(
                success=True,
                data=data,
                summary=f"{method} {url} → {response.status_code}",
            )

        except httpx.TimeoutException:
            logger.warning(f"← TIMEOUT: {method} {url} (after {self.timeout}s)")
            return ToolResult(
                success=False,
                error=f"Request timed out after {self.timeout}s: {method} {url}",
            )
        except httpx.ConnectError:
            logger.error(f"← CONNECT_ERROR: {method} {url} (gateway: {self.gateway_url})")
            return ToolResult(
                success=False,
                error=f"Cannot connect to gateway at {self.gateway_url}. Is nocode-saas running?",
            )
        except Exception as e:
            logger.exception(f"← EXCEPTION: {method} {url}")
            return ToolResult(
                success=False,
                error=f"Unexpected error: {type(e).__name__}: {e}",
            )

    def _error_result(self, response: httpx.Response) -> ToolResult:
        """Build a ToolResult from an HTTP error response."""
        try:
            body = response.json()
            # nocode-saas error format: {"message": "...", "data": {...}}
            message = body.get("message", response.text[:500])
        except Exception:
            message = response.text[:500] if response.text else f"HTTP {response.status_code}"

        return ToolResult(
            success=False,
            error=f"HTTP {response.status_code}: {message}",
        )
