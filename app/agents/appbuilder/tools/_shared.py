"""Shared utilities for appbuilder tools.

Provides the SaasClient singleton and common helpers used by all tools.
"""

from __future__ import annotations

import re

from app.core.tools.base import ToolResult
from app.core.tools.http_client import SaasClient

_NAME_RE = re.compile(r"^[a-zA-Z]+$")


def require_app_code(context: dict) -> tuple[str, ToolResult | None]:
    """Extract appCode from context, returning an error if missing.

    Returns:
        Tuple of (app_code, error). If error is not None, the tool should return it immediately.
    """
    app_code = context.get("app_code", "")
    if not app_code:
        return "", ToolResult(
            success=False,
            error="No appCode set. Use list_applications first to search for the application and determine its appCode before calling this tool.",
        )
    return app_code, None


def validate_name(name: str) -> ToolResult | None:
    """Validate an entity name — must contain only letters (a-z, A-Z).

    Returns a ToolResult with an error if invalid, or None if valid.
    """
    if not name:
        return ToolResult(success=False, error="Name must not be empty.")
    if not _NAME_RE.match(name):
        return ToolResult(
            success=False,
            error=f"Invalid name '{name}'. Names must contain only alphabetic characters (a-z, A-Z), no numbers, spaces, or special characters.",
        )
    return None

async def save_entity(
    client: SaasClient,
    api_path: str,
    entity_id: str,
    entity_data: dict,
    headers: dict[str, str],
    user_client_code: str,
    message: str = "",
) -> ToolResult:
    """Save an entity with override-awareness.

    If the entity's ``clientCode`` matches the user's client, performs a normal
    PUT update.  Otherwise the user is editing a shared object — strip the
    ``id`` and POST so the backend creates an override for the user's client.

    Callers should set ``entity_data["message"]`` to the commit message before
    calling this function (or pass it via the ``message`` argument).
    """
    # Pass through the message set by the caller; allow override via arg
    entity_data = {**entity_data, "message": message or entity_data.get("message", "")}

    object_client = entity_data.get("clientCode", "")

    if object_client and object_client != user_client_code:
        # Editing another client's object → create override (POST without id)
        override_data = {k: v for k, v in entity_data.items() if k != "id"}
        result = await client.post(api_path, headers=headers, json=override_data)
    else:
        # Own object → normal update
        result = await client.put(f"{api_path}/{entity_id}", headers=headers, json=entity_data)

    if not result.success:
        return ToolResult(success=False, error=f"Failed to save: {result.error}")

    return ToolResult(success=True, data=result.data)


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
