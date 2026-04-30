"""OAuth token fetcher — talks to the nocode-saas connection service.

Ported from ``ds/oserver/services/connection.py``. Key adaptation: auth headers
are passed in explicitly instead of pulled from a global ContextVar. This file
is used by the Google Ads and Meta adapters to fetch per-user access tokens.
"""

from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class TokenServiceError(RuntimeError):
    pass


def _ci_get(headers: dict[str, str], key: str) -> str | None:
    """Case-insensitive header lookup."""
    if not headers:
        return None
    target = key.lower()
    for k, v in headers.items():
        if k.lower() == target:
            return v
    return None


async def fetch_oauth_token(
    connection_name: str,
    client_code: str,
    auth_headers: dict[str, str],
    *,
    appcode: str | None = None,
    timeout: float = 15.0,
) -> str:
    """Fetch an OAuth token from nocode-saas by connection name.

    ``auth_headers`` comes from the tool context (Authorization, X-Forwarded-Host,
    X-Forwarded-Port, appCode). We forward whatever the calling user presented,
    which is how ds authenticates inbound requests today. Header lookup is
    case-insensitive — incoming headers arrive in varying cases depending on
    the transport.
    """
    url = f"{settings.GATEWAY_URL}/api/core/connections/internal/oauth2/token/{connection_name}"
    headers = {
        "appcode": appcode or _ci_get(auth_headers, "appCode") or "marketingai",
        "clientcode": client_code,
    }
    logger.info(
        "fetch_oauth_token: connection=%s client_code=%r appcode=%r auth_headers_keys=%s",
        connection_name, client_code, headers["appcode"],
        sorted([k.lower() for k in (auth_headers or {}).keys()]),
    )
    auth = _ci_get(auth_headers, "Authorization")
    if auth:
        headers["authorization"] = auth
    fwd_host = _ci_get(auth_headers, "X-Forwarded-Host")
    if fwd_host:
        headers["X-Forwarded-Host"] = fwd_host
    fwd_port = _ci_get(auth_headers, "X-Forwarded-Port")
    if fwd_port:
        headers["X-Forwarded-Port"] = fwd_port

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.error("token_service_failed: connection=%s err=%s", connection_name, str(e)[:200])
        raise TokenServiceError(f"Token service failed for {connection_name}") from e

    try:
        data = resp.json()
        token = data.get("access_token") or data.get("token") or data.get("id_token")
        if not token:
            logger.warning(
                "token_service_empty_json: connection=%s keys=%s body=%s",
                connection_name, list(data.keys()) if isinstance(data, dict) else type(data).__name__,
                resp.text[:200],
            )
            token = resp.text.strip()
    except ValueError:
        token = resp.text.strip()

    if not token:
        logger.warning(
            "token_service_returned_empty: connection=%s status=%d body=%s",
            connection_name, resp.status_code, resp.text[:200],
        )
    return token


async def fetch_google_api_token(
    client_code: str, auth_headers: dict[str, str],
) -> str:
    return await fetch_oauth_token("GOOGLE_API", client_code, auth_headers)


async def fetch_meta_api_token(
    client_code: str, auth_headers: dict[str, str],
) -> str:
    return await fetch_oauth_token("META_API", client_code, auth_headers)
