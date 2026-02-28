"""Shared utilities for appbuilder tools.

Provides the SaasClient singleton used by all tools.
"""

from __future__ import annotations

from app.core.tools.http_client import SaasClient

_client: SaasClient | None = None


def get_saas_client() -> SaasClient:
    """Get the shared SaasClient singleton."""
    global _client
    if _client is None:
        from app.config import settings
        _client = SaasClient(settings.GATEWAY_URL)
    return _client


async def close_saas_client() -> None:
    """Close the SaasClient (call on shutdown)."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None
