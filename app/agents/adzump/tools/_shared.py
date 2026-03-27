"""Shared utilities for adzump tools.

Provides the DsClient singleton for calling the ds (Adzump) service,
and common helpers used across adzump tools.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.tools.base import ToolResult
from app.core.tools.http_client import SaasClient

logger = logging.getLogger(__name__)

_ds_client: SaasClient | None = None


def get_ds_client() -> SaasClient:
    """Get the shared DsClient singleton for calling ds service."""
    global _ds_client
    if _ds_client is None:
        from app.config import settings
        ds_url = getattr(settings, "DS_SERVICE_URL", "http://localhost:5002")
        _ds_client = SaasClient(ds_url, timeout=120.0)
    return _ds_client


async def close_ds_client() -> None:
    """Close the DsClient (call on shutdown)."""
    global _ds_client
    if _ds_client is not None:
        await _ds_client.close()
        _ds_client = None


def build_ds_headers(context: dict) -> dict[str, str]:
    """Build HTTP headers for ds service calls from tool context.

    Forwards auth token and client code so ds can authenticate
    with Google Ads / Meta APIs on behalf of the user.
    """
    headers = {}
    if "headers" in context:
        # Forward all auth headers from the session
        headers.update(context["headers"])
    if "client_code" in context:
        headers["clientCode"] = context["client_code"]
    return headers


def require_campaign_data(context: dict, *fields: str) -> ToolResult | None:
    """Validate that required campaign data fields exist in session context.

    Returns a ToolResult error if any field is missing, or None if all present.
    """
    campaign = context.get("session_context", {}).get("campaign_data", {})
    missing = [f for f in fields if not campaign.get(f)]
    if missing:
        return ToolResult(
            success=False,
            error=f"Missing required campaign data: {', '.join(missing)}. "
                  "Collect this information from the user first.",
        )
    return None
