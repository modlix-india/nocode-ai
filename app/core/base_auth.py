"""Shared authentication dependencies for all agent routers.

Provides FastAPI Depends()-based auth that extracts tokens, validates
via the security service, and returns an AuthContext.
"""

from __future__ import annotations

import logging

from app.config import settings
from fastapi import HTTPException, Request

from app.core.session import AuthContext

logger = logging.getLogger(__name__)


def _extract_token(request: Request) -> str:
    """Extract auth token from Authorization header or cookie fallback."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header:
        return auth_header
    cookie_token = request.cookies.get("Authorization", "")
    if cookie_token:
        return f"Bearer {cookie_token}"
    return ""


def _extract_forwarded_headers(request: Request) -> tuple[str, str]:
    """Extract X-Forwarded-Host/Port from request (set by gateway/proxy)."""
    host = request.headers.get(
        "X-Forwarded-Host", request.url.hostname or "localhost"
    )
    port = request.headers.get(
        "X-Forwarded-Port", str(request.url.port or 80)
    )
    if "," in port:
        port = port.split(",")[0]
    return host, port


async def _authenticate(
    request: Request,
    auth_header: str,
    client_code: str,
    access_app_code: str,
) -> AuthContext:
    """Validate token via security service and build AuthContext."""
    from app.services.security import get_context_authentication

    ctx_auth = await get_context_authentication(
        request=request,
        authorization=auth_header,
        client_code=client_code,
        app_code=access_app_code,
    )
    if not ctx_auth.isAuthenticated:
        raise HTTPException(status_code=401, detail="Not authenticated")

    forwarded_host, forwarded_port = _extract_forwarded_headers(request)

    # The security context is the only source of truth for tenant + identity.
    # The URL/header clientCode is provider-side (gateway-injected from the URL
    # path) and reflects which tenant the user is browsing, not who they are.
    # Saas's security service resolves the user's actual tenant via DB lookup
    # of the JWT's userId → clientId → client.code and surfaces it as the
    # top-level `clientCode` on ContextAuthentication (set from typ.getT2() in
    # AuthenticationService.makeSpringAuthentication). Note: user.clientCode
    # is NOT populated by saas's User.toContextUser() - only user.clientId is.
    if not ctx_auth.clientCode:
        raise HTTPException(
            status_code=401,
            detail="Security context did not return a resolved clientCode",
        )

    return AuthContext(
        token=auth_header,
        client_code=ctx_auth.clientCode,
        client_id=(ctx_auth.user.clientId if ctx_auth.user else 0) or 0,
        user_id=(ctx_auth.user.id if ctx_auth.user else 0) or 0,
        app_code=access_app_code,
        access_app_code=access_app_code,
        forwarded_host=forwarded_host,
        forwarded_port=forwarded_port,
    )

async def require_auth_context(request: Request) -> AuthContext:
    """FastAPI dependency: extract headers, validate token, return AuthContext."""
    auth_header = _extract_token(request)
    client_code = request.headers.get("clientCode", "")
    access_app_code = request.headers.get("appCode", "")

    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header or token cookie")
    
    # Standalone/dev: webpack proxy forwards X-Path-Prefix (/{appCode}/{clientCode}/page)
    # instead of injecting clientCode/appCode headers as the gateway does in production.
    if settings.STANDALONE_MODE and (not client_code or not access_app_code):
        parts = [p for p in request.headers.get("X-Path-Prefix", "").strip("/").split("/") if p]
        if len(parts) >= 2 and not client_code:
            client_code = parts[1]
        if len(parts) >= 1 and not access_app_code:
            access_app_code = parts[0]

    if not client_code:
        raise HTTPException(status_code=400, detail="Missing clientCode header")

    return await _authenticate(request, auth_header, client_code, access_app_code)
