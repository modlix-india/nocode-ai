"""Shared utilities for appbuilder tools.

Provides the SaasClient singleton and common helpers used by all tools.
"""

from __future__ import annotations

import re

from app.core.tools.base import ToolResult
from app.core.tools.http_client import SaasClient

_NAME_RE = re.compile(r"^[a-zA-Z]+$")

# Re-exported so tool modules import their app-scope keys from one place. The
# definitions live in core because the session, the run loop and per-app
# services (KB, lore) all key off them too — see `app.core.session`.
from app.core.session import FOCUS_APP_KEY, SEEN_APPS_KEY  # noqa: E402


def resolve_app_code(params: dict, context: dict) -> str:
    """The app a tool call actually targets.

    Order: the explicit ``app_code`` argument, then the session's focus app,
    then the app the chat request opened with.

    The focus step is the fix for a real production failure. A session started
    from appbuilder's own page carries ``app_code="appbuilder"`` for its whole
    life, because nothing used to move it. An agent told "build a CRM" would
    create the `crm` app, build its pages with an explicit ``app_code``, then
    drop that optional argument on a later patch — and the call resolved to
    `appbuilder`, where those pages do not exist. One batch of 13 parallel
    patches died that way in a single message. Preferring the app writes have
    been landing in makes the omission harmless instead of fatal.

    Reads deliberately do not set the focus (see
    ``AppBuilderAgent.note_tool_outcome``), so reading another app's page as an
    example cannot hijack where the next edit goes.
    """
    explicit = (params.get("app_code") or "").strip() if isinstance(params, dict) else ""
    if explicit:
        return explicit
    focus = (context.get(FOCUS_APP_KEY) or "").strip() if isinstance(context, dict) else ""
    if focus:
        return focus
    return (context.get("app_code") or "").strip() if isinstance(context, dict) else ""


def app_scope_hint(context: dict, app_code: str) -> str:
    """Suffix for a "not found in app X" error, naming other candidate apps.

    Empty unless this session has written to an app other than the one that was
    searched — in which case the omission of ``app_code`` is the likeliest
    explanation for the miss, and saying so saves the agent a guess.
    """
    if not isinstance(context, dict):
        return ""
    others = [
        a for a in (context.get(SEEN_APPS_KEY) or [])
        if isinstance(a, str) and a and a != app_code
    ]
    if not others:
        return ""
    listed = ", ".join(f"'{a}'" for a in others)
    return (
        f" This session has also written to {listed}. If you meant one of those,"
        " pass `app_code` explicitly; it is not inferred from the object name."
    )


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
