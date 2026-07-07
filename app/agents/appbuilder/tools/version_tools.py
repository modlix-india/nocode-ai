"""Version history and rollback tools.

UI versions: /api/ui/versions/{objectId}/query
Core versions: /api/core/versions/{objectId}/query

The versioning system stores complete snapshots of each version.
Rollback is done by fetching a historical version's ``object`` field
and PUTting it back to the entity endpoint.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.tools.base import ToolDefinition, ToolResult, ToolParameter
from app.core.tools.http_client import SaasClient


def _get_client_and_headers(context: dict[str, Any]) -> tuple[SaasClient, dict[str, str]]:
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context["headers"]


# Map entity type names to their API base paths and version service prefix.
_ENTITY_API_MAP: dict[str, tuple[str, str]] = {
    # UI service entities
    "application": ("/api/ui/applications", "/api/ui/versions"),
    "page": ("/api/ui/pages", "/api/ui/versions"),
    "theme": ("/api/ui/themes", "/api/ui/versions"),
    "style": ("/api/ui/styles", "/api/ui/versions"),
    "function": ("/api/ui/functions", "/api/ui/versions"),
    "schema": ("/api/ui/schemas", "/api/ui/versions"),
    "filler": ("/api/ui/filler", "/api/ui/versions"),
    "uripath": ("/api/ui/uripaths", "/api/ui/versions"),
    # Core service entities
    "connection": ("/api/core/connections", "/api/core/versions"),
    "workflow": ("/api/core/workflows", "/api/core/versions"),
    "template": ("/api/core/templates", "/api/core/versions"),
}


# ── list_versions ───────────────────────────────────────────────

async def _list_versions_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    object_id = params["object_id"]
    entity_type = params.get("entity_type", "page")
    page = params.get("page", 0)
    size = params.get("size", 20)

    entry = _ENTITY_API_MAP.get(entity_type)
    if not entry:
        return ToolResult(success=False, error=f"Unknown entity type '{entity_type}'. Valid: {', '.join(_ENTITY_API_MAP)}")

    _, version_prefix = entry

    result = await client.post(
        f"{version_prefix}/{object_id}/query",
        headers=headers,
        json={"page": page, "size": size},
    )
    if not result.success:
        return ToolResult(success=False, error=f"Failed to list versions: {result.error}")

    data = result.data
    versions = data.get("content", []) if isinstance(data, dict) else []
    total = data.get("totalElements", len(versions)) if isinstance(data, dict) else len(versions)

    lines = []
    for v in versions:
        vnum = v.get("versionNumber", "?")
        msg = v.get("message", "")
        created = v.get("createdAt", "?")
        vid = v.get("id", "?")
        label = f"v{vnum}"
        if msg:
            label += f" - {msg}"
        lines.append(f"- {label} (id={vid}, created={created})")

    summary = f"Found {total} version(s) for {entity_type} '{object_id}':\n" + "\n".join(lines)
    return ToolResult(
        success=True,
        data=[{"id": v.get("id"), "versionNumber": v.get("versionNumber"), "message": v.get("message"), "createdAt": v.get("createdAt")} for v in versions],
        summary=summary,
    )


list_versions = ToolDefinition(
    name="list_versions",
    display_name="List Versions",
    description="List version history for a UI or core entity. Returns version numbers, messages, and IDs.",
    parameters=[
        ToolParameter(name="object_id", type="string", description="The entity's ID whose versions to list."),
        ToolParameter(name="entity_type", type="string", description="Entity type: page, application, theme, style, function, schema, filler, uripath, connection, workflow, template."),
        ToolParameter(name="page", type="number", description="Page number (0-indexed).", required=False),
        ToolParameter(name="size", type="number", description="Page size.", required=False),
    ],
    execute=_list_versions_execute,
)


# ── read_version ────────────────────────────────────────────────

async def _read_version_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    version_id = params["version_id"]
    entity_type = params.get("entity_type", "page")

    entry = _ENTITY_API_MAP.get(entity_type)
    if not entry:
        return ToolResult(success=False, error=f"Unknown entity type '{entity_type}'.")

    _, version_prefix = entry

    result = await client.get(f"{version_prefix}/{version_id}", headers=headers)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to read version: {result.error}")

    version_data = result.data
    vnum = version_data.get("versionNumber", "?") if isinstance(version_data, dict) else "?"
    obj = version_data.get("object", {}) if isinstance(version_data, dict) else {}

    return ToolResult(
        success=True,
        data=version_data,
        summary=f"Version v{vnum}:\n{json.dumps(obj, indent=2, default=str)[:2000]}",
    )


read_version = ToolDefinition(
    name="read_version",
    display_name="Read Version",
    description="Read a specific version snapshot. Returns the full object state at that version.",
    parameters=[
        ToolParameter(name="version_id", type="string", description="Version document ID (from list_versions)."),
        ToolParameter(name="entity_type", type="string", description="Entity type (page, application, theme, etc.)."),
    ],
    execute=_read_version_execute,
)


# ── rollback_version ───────────────────────────────────────────

async def _rollback_version_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    version_id = params["version_id"]
    entity_type = params.get("entity_type", "page")
    message = params["message"]

    entry = _ENTITY_API_MAP.get(entity_type)
    if not entry:
        return ToolResult(success=False, error=f"Unknown entity type '{entity_type}'.")

    entity_prefix, version_prefix = entry

    # 1. Fetch the version snapshot
    version_result = await client.get(f"{version_prefix}/{version_id}", headers=headers)
    if not version_result.success:
        return ToolResult(success=False, error=f"Failed to read version: {version_result.error}")

    version_data = version_result.data
    historical_object = version_data.get("object") if isinstance(version_data, dict) else None
    if not historical_object:
        return ToolResult(success=False, error="Version snapshot has no object data.")

    object_id = historical_object.get("id") or (version_data.get("objectName") if isinstance(version_data, dict) else None)
    if not object_id:
        return ToolResult(success=False, error="Cannot determine entity ID from version snapshot.")

    # 2. Fetch the current entity to get the latest version number
    current_result = await client.get(f"{entity_prefix}/{object_id}", headers=headers)
    if not current_result.success:
        return ToolResult(success=False, error=f"Failed to read current entity: {current_result.error}")

    # 3. Restore: use the historical object but keep the current version number
    restored = {**historical_object}
    restored["version"] = current_result.data.get("version", 1) if isinstance(current_result.data, dict) else 1
    restored["message"] = message

    # 4. Save with override-awareness
    from app.agents.appbuilder.tools._shared import save_entity
    put_result = await save_entity(client, entity_prefix, object_id, restored, headers, context.get("client_code", ""))
    if not put_result.success:
        return put_result

    vnum = version_data.get("versionNumber", "?") if isinstance(version_data, dict) else "?"
    return ToolResult(
        success=True,
        summary=f"Rolled back {entity_type} '{object_id}' to version v{vnum}.",
    )


rollback_version = ToolDefinition(
    name="rollback_version",
    display_name="Rollback Version",
    description="Rollback an entity to a previous version. Fetches the historical snapshot and PUTs it back, creating a new version.",
    parameters=[
        ToolParameter(name="version_id", type="string", description="Version document ID to restore (from list_versions)."),
        ToolParameter(name="entity_type", type="string", description="Entity type (page, application, theme, etc.)."),
        ToolParameter(name="message", type="string", description="Commit message (10–15 words) describing what was changed."),
    ],
    execute=_rollback_version_execute,
)


# ── Export ───────────────────────────────────────────────────────

VERSION_TOOLS: list[ToolDefinition] = [
    list_versions,
    read_version,
    rollback_version,
]
