"""Messaging & event entities — notifications, connections, templates, events.

Ports modlix-mcp/modlix_mcp/tools/{notifications,connections,templates,events}.py
— 28 tools total (notifications:6, connections:5, templates:7, events:10).

  - **notifications**: /api/core/notifications — named events with per-channel
                       × per-locale templated parts (inapp/email/sms)
  - **connections**:   /api/core/connections — external API/messaging integrations
                       (REST_API, SMTP, WHATSAPP, EXOTEL). SECRETS REDACTED.
  - **templates**:     /api/core/templates — multi-locale message bodies for
                       email/SMS/push; metadata vs body reads are split.
  - **events**:        /api/core/{eventDefinitions,eventActions} — declare a
                       named event + payload schema, then handle it with a
                       task pipeline (usually CALL_CORE_FUNCTION).
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

from . import _conventions as c


# Shared param-description constants.
_DESC_APP_CODE = "appCode; defaults to session"
_DESC_CLIENT_CODE = "clientCode; defaults to session"
_DESC_COMMIT_MSG = "Commit message"
_DESC_SIZE = "Max rows"


def _client_and_headers(context: dict[str, Any]) -> tuple[Any, dict[str, str]]:
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context.get("headers") or {}


def _resolve_app_code(params: dict[str, Any], context: dict[str, Any]) -> str:
    return (params.get("app_code") or context.get("app_code") or "").strip()


def _resolve_client_code(params: dict[str, Any], context: dict[str, Any]) -> str:
    return params.get("client_code") or context.get("client_code", "") or ""


def _page_size(params: dict[str, Any], default: int = 100, cap: int = 1000) -> int:
    try:
        return max(1, min(int(params.get("size") or default), cap))
    except (TypeError, ValueError):
        return default


async def _find_by_name(
    client: Any, headers: dict[str, str], api: str, app_code: str, name: str,
) -> tuple[dict[str, Any] | None, str | None]:
    r = await client.get(api, headers=headers, params={"page": 0, "size": 1, "appCode": app_code, "name": name})
    if not r.success:
        return None, r.error
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    if not content:
        return None, f"not found in app '{app_code}'."
    detail = await client.get(f"{api}/{content[0].get('id')}", headers=headers)
    if not detail.success:
        return None, detail.error
    return (detail.data if isinstance(detail.data, dict) else {}), None


def _err_app_code() -> ToolResult:
    return ToolResult(success=False, error="`app_code` is required (set in context or pass explicitly).")


# ═════════════════════════════════════════════════════════════════════════
#  NOTIFICATIONS (6 tools)
# ═════════════════════════════════════════════════════════════════════════

_NOTIF_API = "/api/core/notifications"


async def _execute_list_notifications(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    r = await client.get(_NOTIF_API, headers=headers, params={"page": 0, "size": _page_size(params, 100, 500), "appCode": ac})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    rows = [{
        "name": n.get("name"), "id": n.get("id"), "version": n.get("version"),
        "clientCode": n.get("clientCode"),
        "notificationType": n.get("notificationType"),
        "defaultLanguage": n.get("defaultLanguage"),
        "channels": list((n.get("channelTemplates") or {}).keys()),
    } for n in content]
    return ToolResult(success=True, summary=f"Notifications in '{ac}' ({len(rows)}):\n{json.dumps(rows, indent=2, default=str)}")


list_notifications_tool = ToolDefinition(
    name="list_notifications",
    description="List named notifications in an app with channels + version.",
    parameters=[
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="size", type="integer", required=False, default=100, description=_DESC_SIZE),
    ],
    execute=_execute_list_notifications,
)


async def _execute_get_notification(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, _NOTIF_API, ac, name)
    if err or doc is None:
        return ToolResult(success=False, error=f"notification '{name}' {err or 'not found'}")
    return ToolResult(success=True, summary=json.dumps(doc, indent=2, default=str))


get_notification_tool = ToolDefinition(
    name="get_notification",
    description="Read a notification's full config including channelTemplates (per-channel × per-locale parts).",
    parameters=[
        ToolParameter(name="name", type="string", description="Notification name"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ],
    execute=_execute_get_notification,
)


async def _execute_create_notification(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    notification_type = (params.get("notification_type") or "").strip()
    default_language = (params.get("default_language") or "").strip()
    channel_templates = params.get("channel_templates") or {}
    if not name or not notification_type or not default_language or not isinstance(channel_templates, dict):
        return ToolResult(success=False, error="`name`, `notification_type`, `default_language`, `channel_templates` are required")
    ne = c.validate_simple_name(name)
    if ne:
        return ToolResult(success=False, error=ne)
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    cc = _resolve_client_code(params, context)
    if not any(
        isinstance(ch, dict) and isinstance(ch.get("templateParts"), dict) and default_language in ch["templateParts"]
        for ch in channel_templates.values()
    ):
        return ToolResult(success=False, error=f"default_language '{default_language}' must appear in at least one channel's templateParts.")
    body = {
        "name": name, "appCode": ac, "clientCode": cc,
        "notificationType": notification_type,
        "defaultLanguage": default_language,
        "channelTemplates": channel_templates,
        "message": params.get("message") or "Created via CFA",
    }
    client, headers = _client_and_headers(context)
    r = await client.post(_NOTIF_API, headers=headers, json=body)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Created notification '{name}' ({notification_type}) across channels: {list(channel_templates.keys())}.")


create_notification_tool = ToolDefinition(
    name="create_notification",
    description="Create a notification with per-channel × per-locale templated parts. Common channels: 'inapp', 'email', 'sms'. default_language must appear in at least one channel.",
    parameters=[
        ToolParameter(name="name", type="string", description="Notification name (letters/digits)"),
        ToolParameter(name="notification_type", type="string", description="Severity tag: 'INFO' | 'WARN' | 'ERROR'"),
        ToolParameter(name="default_language", type="string", description="Fallback locale, e.g. 'en'"),
        ToolParameter(name="channel_templates", type="object", description="Per-channel per-locale templates: {channel: {templateParts: {locale: {title, description, image?, ...}}}}"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description=_DESC_CLIENT_CODE),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default="Created via CFA"),
    ],
    execute=_execute_create_notification,
)


async def _execute_update_notification(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, _NOTIF_API, ac, name)
    if err or doc is None:
        return ToolResult(success=False, error=f"notification '{name}' {err or 'not found'}")
    changed: list[str] = []
    if params.get("notification_type") is not None:
        doc["notificationType"] = params["notification_type"]
        changed.append("notificationType")
    if params.get("default_language") is not None:
        doc["defaultLanguage"] = params["default_language"]
        changed.append("defaultLanguage")
    if params.get("channel_templates") is not None:
        doc["channelTemplates"] = params["channel_templates"]
        changed.append("channelTemplates (replaced)")
    if not changed:
        return ToolResult(success=True, summary="No-op: nothing to update.")
    doc["message"] = params.get("message") or "Updated via CFA"
    save = await client.put(f"{_NOTIF_API}/{doc.get('id')}", headers=headers, json=doc)
    if not save.success:
        return ToolResult(success=False, error=save.error)
    return ToolResult(success=True, summary=f"Updated notification '{name}': {', '.join(changed)}.")


update_notification_tool = ToolDefinition(
    name="update_notification",
    description="Replace notification metadata and/or the entire channelTemplates map. For surgical per-(channel,locale) edits, use set_notification_channel_part.",
    parameters=[
        ToolParameter(name="name", type="string", description="Notification name"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="notification_type", type="string", required=False, description="New severity tag"),
        ToolParameter(name="default_language", type="string", required=False, description="New fallback locale"),
        ToolParameter(name="channel_templates", type="object", required=False, description="REPLACE channelTemplates entirely"),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default="Updated via CFA"),
    ],
    execute=_execute_update_notification,
)


async def _execute_set_notification_channel_part(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    channel = (params.get("channel") or "").strip()
    locale = (params.get("locale") or "").strip()
    if not name or not channel or not locale:
        return ToolResult(success=False, error="`name`, `channel`, `locale` are required")
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, _NOTIF_API, ac, name)
    if err or doc is None:
        return ToolResult(success=False, error=f"notification '{name}' {err or 'not found'}")
    ct = dict(doc.get("channelTemplates") or {})
    ch = dict(ct.get(channel) or {})
    parts = dict(ch.get("templateParts") or {})
    part = dict(parts.get(locale) or {})
    if params.get("title") is not None:
        part["title"] = params["title"]
    if params.get("description") is not None:
        part["description"] = params["description"]
    if params.get("image") is not None:
        part["image"] = params["image"]
    if params.get("extra"):
        part.update(params["extra"])
    parts[locale] = part
    ch["templateParts"] = parts
    ct[channel] = ch
    doc["channelTemplates"] = ct
    doc["message"] = params.get("message") or "Updated notification channel part via CFA"
    save = await client.put(f"{_NOTIF_API}/{doc.get('id')}", headers=headers, json=doc)
    if not save.success:
        return ToolResult(success=False, error=save.error)
    return ToolResult(success=True, summary=f"Updated notification '{name}' [{channel}/{locale}].")


set_notification_channel_part_tool = ToolDefinition(
    name="set_notification_channel_part",
    description="Add or replace ONE (channel, locale) part on a notification. Surgical: other channels and locales are preserved.",
    parameters=[
        ToolParameter(name="name", type="string", description="Notification name"),
        ToolParameter(name="channel", type="string", description="Channel key, e.g. 'inapp', 'email', 'sms'"),
        ToolParameter(name="locale", type="string", description="Locale, e.g. 'en', 'hi', 'ar'"),
        ToolParameter(name="title", type="string", required=False, description="Locale-specific title"),
        ToolParameter(name="description", type="string", required=False, description="Locale-specific body (markdown often accepted)"),
        ToolParameter(name="image", type="string", required=False, description="Image path, typically '/api/files/static/file/<client>/<app>/notifications/<file>'"),
        ToolParameter(name="extra", type="object", required=False, description="Additional fields (icon, link, ctaLabel, etc.)"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default="Updated notification channel part via CFA"),
    ],
    execute=_execute_set_notification_channel_part,
)


async def _execute_delete_notification(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, _NOTIF_API, ac, name)
    if err or doc is None:
        return ToolResult(success=False, error=f"notification '{name}' {err or 'not found'}")
    d = await client.delete(f"{_NOTIF_API}/{doc.get('id')}", headers=headers)
    if not d.success:
        return ToolResult(success=False, error=d.error)
    return ToolResult(success=True, summary=f"Deleted notification '{name}' (id={doc.get('id')}).")


delete_notification_tool = ToolDefinition(
    name="delete_notification",
    description="Delete a notification. Triggers that fire it will fail at runtime.",
    parameters=[
        ToolParameter(name="name", type="string", description="Notification name to delete"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ],
    execute=_execute_delete_notification,
)


# ═════════════════════════════════════════════════════════════════════════
#  CONNECTIONS (5 tools) — SECRETS REDACTED in reads
# ═════════════════════════════════════════════════════════════════════════

_CONN_API = "/api/core/connections"
_SECRET_FIELD_RE = re.compile(
    r"(password|secret|token|apiKey|api_key|privateKey|private_key|sessionKey|clientSecret|authToken|accessToken|refreshToken|signature)",
    re.IGNORECASE,
)


def _redact_secrets(details: Any) -> Any:
    if isinstance(details, dict):
        return {k: ("<REDACTED>" if _SECRET_FIELD_RE.search(k) else _redact_secrets(v))
                for k, v in details.items()}
    if isinstance(details, list):
        return [_redact_secrets(x) for x in details]
    return details


async def _execute_list_connections(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    r = await client.get(_CONN_API, headers=headers, params={"page": 0, "size": _page_size(params, 200, 1000), "appCode": ac})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    if params.get("connection_type"):
        content = [x for x in content if x.get("connectionType") == params["connection_type"]]
    rows = [{
        "name": x.get("name"), "id": x.get("id"), "version": x.get("version"),
        "clientCode": x.get("clientCode"),
        "connectionType": x.get("connectionType"),
        "connectionSubType": x.get("connectionSubType"),
        "isAppLevel": x.get("isAppLevel"),
        "onlyThruKIRun": x.get("onlyThruKIRun"),
    } for x in content]
    return ToolResult(success=True, summary=f"Connections in '{ac}' ({len(rows)}):\n{json.dumps(rows, indent=2, default=str)}")


list_connections_tool = ToolDefinition(
    name="list_connections",
    description="List external connections (REST_API, SMTP, WHATSAPP, EXOTEL, ...). No secrets returned.",
    parameters=[
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="connection_type", type="string", required=False, description="Filter by connectionType: CALL | MAIL | NOTIFICATION | REST_API | TEXT"),
        ToolParameter(name="size", type="integer", required=False, default=200, description=_DESC_SIZE),
    ],
    execute=_execute_list_connections,
)


async def _execute_get_connection(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    reveal = bool(params.get("reveal_secrets"))
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, _CONN_API, ac, name)
    if err or doc is None:
        return ToolResult(success=False, error=f"connection '{name}' {err or 'not found'}")
    view = dict(doc)
    if not reveal:
        view["connectionDetails"] = _redact_secrets(view.get("connectionDetails") or {})
    prefix = "" if reveal else "(secrets redacted — pass reveal_secrets=true to see raw)\n"
    return ToolResult(success=True, summary=prefix + json.dumps(view, indent=2, default=str))


get_connection_tool = ToolDefinition(
    name="get_connection",
    description="Read a connection's full config. connectionDetails secrets are redacted unless reveal_secrets=true.",
    parameters=[
        ToolParameter(name="name", type="string", description="Connection name"),
        ToolParameter(name="reveal_secrets", type="boolean", required=False, default=False, description="DANGEROUS: when true, raw credentials returned. Only when actively debugging auth."),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ],
    execute=_execute_get_connection,
)


async def _execute_create_connection(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    connection_type = (params.get("connection_type") or "").strip()
    connection_sub_type = (params.get("connection_sub_type") or "").strip()
    connection_details = params.get("connection_details") or {}
    if not name or not connection_type or not connection_sub_type or not isinstance(connection_details, dict):
        return ToolResult(success=False, error="`name`, `connection_type`, `connection_sub_type`, `connection_details` are required")
    ne = c.validate_simple_name(name)
    if ne:
        return ToolResult(success=False, error=ne)
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    cc = _resolve_client_code(params, context)
    body = {
        "name": name, "appCode": ac, "clientCode": cc,
        "connectionType": connection_type,
        "connectionSubType": connection_sub_type,
        "connectionDetails": connection_details,
        "isAppLevel": bool(params.get("is_app_level")),
        "onlyThruKIRun": bool(params.get("only_thru_kirun")),
        "defaultConnection": bool(params.get("default_connection")),
        "order": int(params.get("order") or 0),
        "message": params.get("message") or "Created via CFA",
    }
    client, headers = _client_and_headers(context)
    r = await client.post(_CONN_API, headers=headers, json=body)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Created connection '{name}' ({connection_type}/{connection_sub_type}).")


create_connection_tool = ToolDefinition(
    name="create_connection",
    description="Create an external connection. REST_API_BASIC: {baseUrl, userName, password}. REST_API_OAUTH2: {baseUrl, clientId, clientSecret, tokenUrl, scopes}. SMTP: {host, port, userName, password, from}. EXOTEL: {accountSid, apiKey, apiToken}. WHATSAPP: provider-specific.",
    parameters=[
        ToolParameter(name="name", type="string", description="Connection name (letters/digits)"),
        ToolParameter(name="connection_type", type="string", description="CALL | MAIL | NOTIFICATION | REST_API | TEXT"),
        ToolParameter(name="connection_sub_type", type="string", description="e.g. REST_API_BASIC, REST_API_OAUTH2, SMTP, EXOTEL, WHATSAPP"),
        ToolParameter(name="connection_details", type="object", description="Subtype-specific config (credentials)"),
        ToolParameter(name="is_app_level", type="boolean", required=False, default=False, description="If true, all pages in the app can use this connection"),
        ToolParameter(name="only_thru_kirun", type="boolean", required=False, default=False, description="Block direct REST access; only Kirun functions may invoke"),
        ToolParameter(name="default_connection", type="boolean", required=False, default=False, description="Mark as default for this type+subType"),
        ToolParameter(name="order", type="integer", required=False, default=0, description="Display/fallback order; lower = earlier"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description=_DESC_CLIENT_CODE),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default="Created via CFA"),
    ],
    execute=_execute_create_connection,
)


async def _execute_update_connection(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, _CONN_API, ac, name)
    if err or doc is None:
        return ToolResult(success=False, error=f"connection '{name}' {err or 'not found'}")
    changed: list[str] = []
    if params.get("connection_type") is not None:
        doc["connectionType"] = params["connection_type"]; changed.append("connectionType")
    if params.get("connection_sub_type") is not None:
        doc["connectionSubType"] = params["connection_sub_type"]; changed.append("connectionSubType")
    if params.get("connection_details") is not None:
        doc["connectionDetails"] = params["connection_details"]; changed.append("connectionDetails")
    if params.get("is_app_level") is not None:
        doc["isAppLevel"] = bool(params["is_app_level"]); changed.append("isAppLevel")
    if params.get("default_connection") is not None:
        doc["defaultConnection"] = bool(params["default_connection"]); changed.append("defaultConnection")
    if params.get("order") is not None:
        doc["order"] = int(params["order"]); changed.append("order")
    if params.get("only_thru_kirun") is not None:
        doc["onlyThruKIRun"] = bool(params["only_thru_kirun"]); changed.append("onlyThruKIRun")
    if not changed:
        return ToolResult(success=True, summary="No-op: nothing to update.")
    doc["message"] = params.get("message") or "Updated via CFA"
    save = await client.put(f"{_CONN_API}/{doc.get('id')}", headers=headers, json=doc)
    if not save.success:
        return ToolResult(success=False, error=save.error)
    return ToolResult(success=True, summary=f"Updated connection '{name}': {', '.join(changed)}.")


update_connection_tool = ToolDefinition(
    name="update_connection",
    description="Update a connection. Pass connection_details only when replacing credentials wholesale (partial credentials are usually wrong).",
    parameters=[
        ToolParameter(name="name", type="string", description="Connection name to update"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="connection_type", type="string", required=False, description="New connectionType"),
        ToolParameter(name="connection_sub_type", type="string", required=False, description="New connectionSubType"),
        ToolParameter(name="connection_details", type="object", required=False, description="REPLACE connectionDetails entirely"),
        ToolParameter(name="is_app_level", type="boolean", required=False, description="Toggle isAppLevel"),
        ToolParameter(name="only_thru_kirun", type="boolean", required=False, description="Toggle onlyThruKIRun"),
        ToolParameter(name="default_connection", type="boolean", required=False, description="Toggle defaultConnection"),
        ToolParameter(name="order", type="integer", required=False, description="New order"),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default="Updated via CFA"),
    ],
    execute=_execute_update_connection,
)


async def _execute_delete_connection(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, _CONN_API, ac, name)
    if err or doc is None:
        return ToolResult(success=False, error=f"connection '{name}' {err or 'not found'}")
    d = await client.delete(f"{_CONN_API}/{doc.get('id')}", headers=headers)
    if not d.success:
        return ToolResult(success=False, error=d.error)
    return ToolResult(success=True, summary=f"Deleted connection '{name}' (id={doc.get('id')}).")


delete_connection_tool = ToolDefinition(
    name="delete_connection",
    description="Delete a connection. Functions/templates using it will fail at call time.",
    parameters=[
        ToolParameter(name="name", type="string", description="Connection name to delete"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ],
    execute=_execute_delete_connection,
)


# ═════════════════════════════════════════════════════════════════════════
#  TEMPLATES (7 tools)
# ═════════════════════════════════════════════════════════════════════════

_TEMPL_API = "/api/core/templates"


async def _execute_list_templates(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    r = await client.get(_TEMPL_API, headers=headers, params={"page": 0, "size": _page_size(params, 200, 1000), "appCode": ac})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    if params.get("template_type"):
        content = [t for t in content if t.get("templateType") == params["template_type"]]
    rows = [{
        "name": t.get("name"), "id": t.get("id"), "version": t.get("version"),
        "clientCode": t.get("clientCode"),
        "templateType": t.get("templateType"),
        "defaultLanguage": t.get("defaultLanguage"),
        "locales": list((t.get("templateParts") or {}).keys()),
    } for t in content]
    return ToolResult(success=True, summary=f"Templates in '{ac}' ({len(rows)}):\n{json.dumps(rows, indent=2, default=str)}")


list_templates_tool = ToolDefinition(
    name="list_templates",
    description="List templates with their channel + locales (no bodies).",
    parameters=[
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="template_type", type="string", required=False, description="Filter by channel, e.g. 'email', 'sms'"),
        ToolParameter(name="size", type="integer", required=False, default=200, description=_DESC_SIZE),
    ],
    execute=_execute_list_templates,
)


async def _execute_get_template_metadata(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, _TEMPL_API, ac, name)
    if err or doc is None:
        return ToolResult(success=False, error=f"template '{name}' {err or 'not found'}")
    meta = {k: v for k, v in doc.items() if k != "templateParts"}
    meta["localesPresent"] = list((doc.get("templateParts") or {}).keys())
    return ToolResult(success=True, summary=f"Template '{name}' (metadata):\n{json.dumps(meta, indent=2, default=str)}")


get_template_metadata_tool = ToolDefinition(
    name="get_template_metadata",
    description="Read a template's metadata WITHOUT bodies (channel, locales, expressions). Use first; then get_template_part for one locale's body.",
    parameters=[
        ToolParameter(name="name", type="string", description="Template name"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ],
    execute=_execute_get_template_metadata,
)


async def _execute_get_template_part(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    name = (params.get("name") or "").strip()
    locale = (params.get("locale") or "en").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, _TEMPL_API, ac, name)
    if err or doc is None:
        return ToolResult(success=False, error=f"template '{name}' {err or 'not found'}")
    parts = doc.get("templateParts") or {}
    part = parts.get(locale)
    if part is None:
        return ToolResult(success=False, error=f"locale '{locale}' not present in template '{name}'. Available: {list(parts.keys())}")
    return ToolResult(success=True, summary=f"Template '{name}' locale '{locale}':\n{json.dumps(part, indent=2, default=str)}")


get_template_part_tool = ToolDefinition(
    name="get_template_part",
    description="Read one locale's body + subject from a template. HTML email bodies can be large.",
    parameters=[
        ToolParameter(name="name", type="string", description="Template name"),
        ToolParameter(name="locale", type="string", required=False, default="en", description="Locale key, e.g. 'en', 'hi', 'ar'"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ],
    execute=_execute_get_template_part,
)


async def _execute_create_template(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    template_type = (params.get("template_type") or "").strip()
    default_language = (params.get("default_language") or "").strip()
    to_expression = params.get("to_expression")
    language_expression = params.get("language_expression")
    template_parts = params.get("template_parts") or {}
    if not all([name, template_type, default_language, to_expression, language_expression]) or not isinstance(template_parts, dict):
        return ToolResult(success=False, error="`name`, `template_type`, `default_language`, `to_expression`, `language_expression`, `template_parts` are required")
    ne = c.validate_simple_name(name)
    if ne:
        return ToolResult(success=False, error=ne)
    if default_language not in template_parts:
        return ToolResult(success=False, error=f"default_language '{default_language}' must be present in template_parts.")
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    cc = _resolve_client_code(params, context)
    body: dict[str, Any] = {
        "name": name, "appCode": ac, "clientCode": cc,
        "templateType": template_type, "defaultLanguage": default_language,
        "toExpression": to_expression, "languageExpression": language_expression,
        "templateParts": template_parts,
        "message": params.get("message") or "Created via CFA",
    }
    if params.get("from_expression") is not None:
        body["fromExpression"] = params["from_expression"]
    if params.get("title") is not None:
        body["title"] = params["title"]
    client, headers = _client_and_headers(context)
    r = await client.post(_TEMPL_API, headers=headers, json=body)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Created template '{name}' ({template_type}, {len(template_parts)} locales).")


create_template_tool = ToolDefinition(
    name="create_template",
    description="Create a multi-locale template for email/SMS/push.",
    parameters=[
        ToolParameter(name="name", type="string", description="Template name (letters/digits)"),
        ToolParameter(name="template_type", type="string", description="Channel: 'email', 'sms', etc."),
        ToolParameter(name="default_language", type="string", description="Fallback locale, e.g. 'en'"),
        ToolParameter(name="to_expression", type="string", description="Kirun expression computing recipient, e.g. '${user.emailId}'"),
        ToolParameter(name="language_expression", type="string", description="Kirun expression picking locale, e.g. 'en' or '${user.localeCode}'"),
        ToolParameter(name="template_parts", type="object", description="Per-locale parts: {locale: {body, subject, ...}}. Must include default_language."),
        ToolParameter(name="from_expression", type="string", required=False, description="Sender expression, e.g. '${app.fromEmail}'"),
        ToolParameter(name="title", type="string", required=False, description="Human-readable display title"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description=_DESC_CLIENT_CODE),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default="Created via CFA"),
    ],
    execute=_execute_create_template,
)


async def _execute_update_template_part(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    locale = (params.get("locale") or "").strip()
    body = params.get("body")
    if not name or not locale or body is None:
        return ToolResult(success=False, error="`name`, `locale`, `body` are required")
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, _TEMPL_API, ac, name)
    if err or doc is None:
        return ToolResult(success=False, error=f"template '{name}' {err or 'not found'}")
    parts = dict(doc.get("templateParts") or {})
    new_part: dict[str, Any] = {"body": body}
    if params.get("subject") is not None:
        new_part["subject"] = params["subject"]
    parts[locale] = new_part
    doc["templateParts"] = parts
    doc["message"] = params.get("message") or "Updated locale via CFA"
    save = await client.put(f"{_TEMPL_API}/{doc.get('id')}", headers=headers, json=doc)
    if not save.success:
        return ToolResult(success=False, error=save.error)
    return ToolResult(success=True, summary=f"Updated template '{name}' locale '{locale}'.")


update_template_part_tool = ToolDefinition(
    name="update_template_part",
    description="Add or replace one locale's body+subject on a template. Surgical: other locales untouched.",
    parameters=[
        ToolParameter(name="name", type="string", description="Template name"),
        ToolParameter(name="locale", type="string", description="Locale to add/replace, e.g. 'en' or 'hi'"),
        ToolParameter(name="body", type="string", description="Message body (HTML for email, plain text for sms). REPLACES existing."),
        ToolParameter(name="subject", type="string", required=False, description="Subject line (for email). Optional for sms."),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default="Updated locale via CFA"),
    ],
    execute=_execute_update_template_part,
)


async def _execute_update_template(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, _TEMPL_API, ac, name)
    if err or doc is None:
        return ToolResult(success=False, error=f"template '{name}' {err or 'not found'}")
    changed: list[str] = []
    field_map = {
        "template_type": ("templateType", "templateType"),
        "default_language": ("defaultLanguage", "defaultLanguage"),
        "to_expression": ("toExpression", "toExpression"),
        "language_expression": ("languageExpression", "languageExpression"),
        "from_expression": ("fromExpression", "fromExpression"),
        "title": ("title", "title"),
        "template_parts": ("templateParts", "templateParts (replaced)"),
    }
    for param_key, (json_key, change_label) in field_map.items():
        if params.get(param_key) is not None:
            doc[json_key] = params[param_key]
            changed.append(change_label)
    if not changed:
        return ToolResult(success=True, summary="No-op: nothing to update.")
    doc["message"] = params.get("message") or "Updated via CFA"
    save = await client.put(f"{_TEMPL_API}/{doc.get('id')}", headers=headers, json=doc)
    if not save.success:
        return ToolResult(success=False, error=save.error)
    return ToolResult(success=True, summary=f"Updated template '{name}': {', '.join(changed)}.")


update_template_tool = ToolDefinition(
    name="update_template",
    description="Update template metadata and/or replace the locales map. For just-one-locale edits prefer update_template_part.",
    parameters=[
        ToolParameter(name="name", type="string", description="Template name"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="template_type", type="string", required=False, description="New channel value"),
        ToolParameter(name="default_language", type="string", required=False, description="New default locale"),
        ToolParameter(name="to_expression", type="string", required=False, description="New recipient expression"),
        ToolParameter(name="language_expression", type="string", required=False, description="New locale-picker expression"),
        ToolParameter(name="from_expression", type="string", required=False, description="New sender expression"),
        ToolParameter(name="title", type="string", required=False, description="New human-readable title"),
        ToolParameter(name="template_parts", type="object", required=False, description="REPLACES the entire locale map"),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default="Updated via CFA"),
    ],
    execute=_execute_update_template,
)


async def _execute_delete_template(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, _TEMPL_API, ac, name)
    if err or doc is None:
        return ToolResult(success=False, error=f"template '{name}' {err or 'not found'}")
    d = await client.delete(f"{_TEMPL_API}/{doc.get('id')}", headers=headers)
    if not d.success:
        return ToolResult(success=False, error=d.error)
    return ToolResult(success=True, summary=f"Deleted template '{name}' (id={doc.get('id')}).")


delete_template_tool = ToolDefinition(
    name="delete_template",
    description="Delete a template. Notifications/functions referencing it will fail at send-time.",
    parameters=[
        ToolParameter(name="name", type="string", description="Template name to delete"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ],
    execute=_execute_delete_template,
)


# ═════════════════════════════════════════════════════════════════════════
#  EVENTS — eventDefinitions + eventActions (10 tools)
# ═════════════════════════════════════════════════════════════════════════

_EVT_DEFS_API = "/api/core/eventDefinitions"
_EVT_ACTS_API = "/api/core/eventActions"


def _normalize_type(schema: dict[str, Any]) -> dict[str, Any]:
    if isinstance(schema.get("type"), str):
        schema["type"] = [schema["type"]]
    return schema


async def _list_events(api: str, params: dict[str, Any], context: dict[str, Any], row_fn: Any, label: str) -> ToolResult:
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    r = await client.get(api, headers=headers, params={"page": 0, "size": _page_size(params, 100, 500), "appCode": ac})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    rows = [row_fn(e) for e in content]
    return ToolResult(success=True, summary=f"{label} in '{ac}' ({len(rows)}):\n{json.dumps(rows, indent=2, default=str)}")


async def _get_event(api: str, params: dict[str, Any], context: dict[str, Any], label: str) -> ToolResult:
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, api, ac, name)
    if err or doc is None:
        return ToolResult(success=False, error=f"{label} '{name}' {err or 'not found'}")
    return ToolResult(success=True, summary=json.dumps(doc, indent=2, default=str))


async def _update_event(api: str, params: dict[str, Any], context: dict[str, Any], patch: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, api, ac, name)
    if err or doc is None:
        return ToolResult(success=False, error=f"'{name}' {err or 'not found'}")
    doc.update(patch)
    doc["message"] = params.get("message") or "Updated via CFA"
    save = await client.put(f"{api}/{doc.get('id')}", headers=headers, json=doc)
    if not save.success:
        return ToolResult(success=False, error=save.error)
    return ToolResult(success=True, summary=f"Updated '{name}'.")


async def _delete_event(api: str, params: dict[str, Any], context: dict[str, Any], label: str) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, api, ac, name)
    if err or doc is None:
        return ToolResult(success=False, error=f"{label} '{name}' {err or 'not found'}")
    d = await client.delete(f"{api}/{doc.get('id')}", headers=headers)
    if not d.success:
        return ToolResult(success=False, error=d.error)
    return ToolResult(success=True, summary=f"Deleted {label} '{name}' (id={doc.get('id')}).")


# ── eventDefinitions ──────────────────────────────────────────────────


def _ev_def_row(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": e.get("name"), "id": e.get("id"), "version": e.get("version"),
        "clientCode": e.get("clientCode"),
        "schemaType": (e.get("schema") or {}).get("type"),
    }


async def _execute_list_event_definitions(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    return await _list_events(_EVT_DEFS_API, params, context, _ev_def_row, "Event definitions")


list_event_definitions_tool = ToolDefinition(
    name="list_event_definitions",
    description="List event definitions in an app (named events + payload schemas).",
    parameters=[
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="size", type="integer", required=False, default=100, description=_DESC_SIZE),
    ],
    execute=_execute_list_event_definitions,
)


async def _execute_get_event_definition(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    return await _get_event(_EVT_DEFS_API, params, context, "event definition")


get_event_definition_tool = ToolDefinition(
    name="get_event_definition",
    description="Read an event definition (name + payload schema).",
    parameters=[
        ToolParameter(name="name", type="string", description="Event name, e.g. 'USER_REGISTERED'"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ],
    execute=_execute_get_event_definition,
)


async def _execute_create_event_definition(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    schema = params.get("schema") or {}
    if not name or not isinstance(schema, dict):
        return ToolResult(success=False, error="`name` and `schema` (dict) are required")
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    cc = _resolve_client_code(params, context)
    _normalize_type(schema)
    body = {
        "name": name, "appCode": ac, "clientCode": cc,
        "schema": schema, "message": params.get("message") or "Created via CFA",
    }
    client, headers = _client_and_headers(context)
    r = await client.post(_EVT_DEFS_API, headers=headers, json=body)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Created event definition '{name}' (id={(r.data or {}).get('id', '?')}).")


create_event_definition_tool = ToolDefinition(
    name="create_event_definition",
    description="Create a named event with its payload schema (usually {type: 'OBJECT', properties: {...}}). Convention: UPPER_SNAKE_CASE name.",
    parameters=[
        ToolParameter(name="name", type="string", description="Event name, e.g. 'USER_REGISTERED'"),
        ToolParameter(name="schema", type="object", description="Kirun schema describing the event payload"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description=_DESC_CLIENT_CODE),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default="Created via CFA"),
    ],
    execute=_execute_create_event_definition,
)


async def _execute_update_event_definition(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    schema = params.get("schema") or {}
    if not isinstance(schema, dict):
        return ToolResult(success=False, error="`schema` is required")
    return await _update_event(_EVT_DEFS_API, params, context, {"schema": _normalize_type(schema)})


update_event_definition_tool = ToolDefinition(
    name="update_event_definition",
    description="Replace an event's payload schema.",
    parameters=[
        ToolParameter(name="name", type="string", description="Event name to update"),
        ToolParameter(name="schema", type="object", description="Replacement payload schema"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default="Updated via CFA"),
    ],
    execute=_execute_update_event_definition,
)


async def _execute_delete_event_definition(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    return await _delete_event(_EVT_DEFS_API, params, context, "event definition")


delete_event_definition_tool = ToolDefinition(
    name="delete_event_definition",
    description="Delete an event definition. Backend rejects if any eventAction or Storage trigger references this event.",
    parameters=[
        ToolParameter(name="name", type="string", description="Event name to delete"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ],
    execute=_execute_delete_event_definition,
)


# ── eventActions ──────────────────────────────────────────────────────


def _ev_act_row(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": e.get("name"), "id": e.get("id"), "version": e.get("version"),
        "clientCode": e.get("clientCode"),
        "taskCount": len(e.get("tasks") or {}),
    }


async def _execute_list_event_actions(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    return await _list_events(_EVT_ACTS_API, params, context, _ev_act_row, "Event actions")


list_event_actions_tool = ToolDefinition(
    name="list_event_actions",
    description="List event actions (event handlers) in an app.",
    parameters=[
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="size", type="integer", required=False, default=100, description=_DESC_SIZE),
    ],
    execute=_execute_list_event_actions,
)


async def _execute_get_event_action(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    return await _get_event(_EVT_ACTS_API, params, context, "event action")


get_event_action_tool = ToolDefinition(
    name="get_event_action",
    description="Read an event action's task pipeline.",
    parameters=[
        ToolParameter(name="name", type="string", description="Event action name (matches an event definition name)"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ],
    execute=_execute_get_event_action,
)


async def _execute_create_event_action(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    tasks = params.get("tasks") or {}
    if not name or not isinstance(tasks, dict):
        return ToolResult(success=False, error="`name` and `tasks` (dict) are required")
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    cc = _resolve_client_code(params, context)
    body = {
        "name": name, "appCode": ac, "clientCode": cc,
        "tasks": tasks, "message": params.get("message") or "Created via CFA",
    }
    client, headers = _client_and_headers(context)
    r = await client.post(_EVT_ACTS_API, headers=headers, json=body)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Created event action '{name}' with {len(tasks)} task(s) (id={(r.data or {}).get('id', '?')}).")


create_event_action_tool = ToolDefinition(
    name="create_event_action",
    description="Create an event handler — a task pipeline that runs when the named event fires. Tasks of type CALL_CORE_FUNCTION reference a server function by (namespace, name); the event payload is passed via functionParameterName.",
    parameters=[
        ToolParameter(name="name", type="string", description="Event name this action handles, e.g. 'USER_REGISTERED'"),
        ToolParameter(name="tasks", type="object", description="Ordered task pipeline: {taskKey: {key, order: int, type: 'CALL_CORE_FUNCTION', parameters: {name, namespace, functionParameterName}}}"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description=_DESC_CLIENT_CODE),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default="Created via CFA"),
    ],
    execute=_execute_create_event_action,
)


async def _execute_update_event_action(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    tasks = params.get("tasks")
    if not isinstance(tasks, dict):
        return ToolResult(success=False, error="`tasks` (dict) is required")
    return await _update_event(_EVT_ACTS_API, params, context, {"tasks": tasks})


update_event_action_tool = ToolDefinition(
    name="update_event_action",
    description="Replace an event action's task pipeline.",
    parameters=[
        ToolParameter(name="name", type="string", description="Event action name to update"),
        ToolParameter(name="tasks", type="object", description="Replacement task map"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default="Updated via CFA"),
    ],
    execute=_execute_update_event_action,
)


async def _execute_delete_event_action(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    return await _delete_event(_EVT_ACTS_API, params, context, "event action")


delete_event_action_tool = ToolDefinition(
    name="delete_event_action",
    description="Delete an event action. The event keeps firing but loses this handler.",
    parameters=[
        ToolParameter(name="name", type="string", description="Event action name to delete"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ],
    execute=_execute_delete_event_action,
)


# ── Module export ────────────────────────────────────────────────────────


TOOLS: list[ToolDefinition] = [
    # notifications (6)
    list_notifications_tool, get_notification_tool, create_notification_tool,
    update_notification_tool, set_notification_channel_part_tool, delete_notification_tool,
    # connections (5)
    list_connections_tool, get_connection_tool, create_connection_tool,
    update_connection_tool, delete_connection_tool,
    # templates (7)
    list_templates_tool, get_template_metadata_tool, get_template_part_tool,
    create_template_tool, update_template_part_tool, update_template_tool, delete_template_tool,
    # events (10)
    list_event_definitions_tool, get_event_definition_tool, create_event_definition_tool,
    update_event_definition_tool, delete_event_definition_tool,
    list_event_actions_tool, get_event_action_tool, create_event_action_tool,
    update_event_action_tool, delete_event_action_tool,
]
