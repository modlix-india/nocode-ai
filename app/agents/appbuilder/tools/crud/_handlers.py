"""Generic CRUD handler functions - dispatches by ObjectTypeConfig.

Reuses shared utilities from _shared.py and _executor.py.
Each handler branches on config flags for entity-specific behaviour.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.tools.base import ToolResult
from app.agents.appbuilder.tools.crud._registry import ObjectTypeConfig


def _get_client_and_headers(context: dict[str, Any]) -> tuple:
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context["headers"]


def _resolve_app_code(params: dict[str, Any], context: dict[str, Any]) -> tuple[str, ToolResult | None]:
    """Resolve app_code from params or context. Returns (app_code, error)."""
    app_code = params.get("app_code") or context.get("app_code", "")
    if not app_code:
        return "", ToolResult(
            success=False,
            error="No appCode set. Use list(object_type='application') first to find the appCode.",
        )
    return app_code, None


# ── LIST ──────────────────────────────────────────────────────────


async def generic_list(
    config: ObjectTypeConfig, params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    """List entities of a given type."""
    # Application uses a special list endpoint
    if config.object_type == "application":
        return await _list_application(params, context)

    # Page has custom list with component counts
    if config.has_page_sub_ops:
        from app.agents.appbuilder.tools.crud.page_ops import page_list
        return await page_list(params, context)

    # Standard: GET with pagination
    client, headers = _get_client_and_headers(context)
    app_code, err = _resolve_app_code(params, context)
    if err:
        return err

    result = await client.get(
        config.api_path,
        headers=headers,
        params={"page": 0, "size": 1000, "appCode": app_code, "eager": "true"},
    )
    if not result.success:
        return ToolResult(success=False, error=f"Failed to list {config.display_name}s: {result.error}")

    data = result.data
    items = data.get("content", []) if isinstance(data, dict) else []

    # Function type includes namespace in summary
    if config.has_namespace:
        lines = [f"- {i.get('name', '?')}.{i.get('namespace', '?')} (id={i.get('id', '?')}, v{i.get('version', '?')})" for i in items]
        item_data = [{"name": i.get("name"), "namespace": i.get("namespace"), "id": i.get("id"), "version": i.get("version")} for i in items]
    else:
        lines = [f"- {i.get('name', '?')} (id={i.get('id', '?')}, v{i.get('version', '?')})" for i in items]
        item_data = [{"name": i.get("name"), "id": i.get("id"), "version": i.get("version")} for i in items]

    return ToolResult(
        success=True,
        data=item_data,
        summary=f"Found {len(items)} {config.display_name}(s):\n" + "\n".join(lines),
    )


async def _list_application(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """List applications via the security service query endpoint."""
    client, headers = _get_client_and_headers(context)

    body: dict[str, Any] = {"page": 0, "size": 100}
    name_filter = params.get("app_code", "")
    if name_filter:
        body["condition"] = {
            "conditions": [
                {"field": "appName", "value": name_filter, "operator": "STRING_LOOSE_EQUAL"},
                {"field": "appCode", "value": name_filter, "operator": "STRING_LOOSE_EQUAL"},
            ],
            "operator": "OR",
        }

    result = await client.post("/api/security/applications/query", headers=headers, json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to list applications: {result.error}")

    data = result.data
    apps = data.get("content", []) if isinstance(data, dict) else []
    lines = [f"- {a.get('appCode', a.get('name', '?'))} (id={a.get('id', '?')}, v{a.get('version', '?')})" for a in apps]

    # Track found app codes in session context
    session_ctx = context.get("session_context")
    if session_ctx is not None and apps:
        found_codes = [a.get("appCode") for a in apps if a.get("appCode")]
        app_codes = session_ctx.setdefault("app_codes", [])
        for code in found_codes:
            if code not in app_codes:
                app_codes.append(code)
        if len(found_codes) == 1:
            session_ctx["app_code"] = found_codes[0]

    return ToolResult(
        success=True,
        data=[{"name": a.get("appCode", a.get("name")), "id": a.get("id"), "version": a.get("version")} for a in apps],
        summary=f"Found {len(apps)} application(s):\n" + "\n".join(lines),
    )


# ── CREATE ────────────────────────────────────────────────────────


async def generic_create(
    config: ObjectTypeConfig, params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    """Create an entity of the given type."""
    # Application has custom create via Multi service
    if config.has_special_create:
        return await _create_application(params, context)

    # Page creates with root Grid
    if config.has_page_sub_ops:
        from app.agents.appbuilder.tools.crud.page_ops import page_create
        return await page_create(params, context)

    # Theme requires confirmation
    if config.requires_confirmation:
        if not params.get("confirmed"):
            return ToolResult(
                success=False,
                error=(
                    "Theme creation affects the entire application. "
                    "You MUST describe the planned theme to the user first, "
                    "then call with confirmed=true after they explicitly agree."
                ),
            )

    from app.agents.appbuilder.tools._shared import validate_name

    client, headers = _get_client_and_headers(context)
    app_code, err = _resolve_app_code(params, context)
    if err:
        return err
    name = params["name"]

    err = validate_name(name)
    if err:
        return err

    body: dict[str, Any] = {
        "name": name,
        "appCode": app_code,
        "clientCode": context.get("client_code", ""),
    }

    # Theme uses variables; others use definition
    if config.has_variables:
        body["variables"] = params.get("variables", {})
    else:
        body["definition"] = params.get("definition", {})

    # Function includes namespace
    if config.has_namespace:
        body["namespace"] = params.get("namespace", "")

    if params.get("title"):
        body["title"] = params["title"]
    if params.get("description"):
        body["description"] = params["description"]
    body["message"] = params["message"]

    api_path = config.create_api_path or config.api_path
    result = await client.post(api_path, headers=headers, json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to create {config.display_name}: {result.error}")

    created = result.data
    entity_id = created.get("id", "?") if isinstance(created, dict) else "?"
    return ToolResult(success=True, summary=f"Created {config.display_name} '{name}' (id={entity_id}).")


async def _create_application(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Create application via the Multi service."""
    from app.agents.appbuilder.tools._shared import validate_name

    client, headers = _get_client_and_headers(context)
    app_name = params["name"]
    app_code = params.get("app_code", "")
    app_type = params.get("app_type", "APP")

    if not app_code:
        return ToolResult(success=False, error="app_code is required when creating an application.")

    err = validate_name(app_code)
    if err:
        return err

    body: dict[str, Any] = {
        "appName": app_name,
        "appCode": app_code,
        "appType": app_type,
    }
    body["message"] = params["message"]

    result = await client.post("/api/multi/application", headers=headers, json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to create application: {result.error}")

    # Track in session context
    session_ctx = context.get("session_context")
    if session_ctx is not None:
        session_ctx["app_code"] = app_code
        app_codes = session_ctx.setdefault("app_codes", [])
        if app_code not in app_codes:
            app_codes.append(app_code)

    return ToolResult(
        success=True,
        data=result.data,
        summary=f"Created application '{app_name}' (code={app_code}, type={app_type}).",
    )


# ── READ ──────────────────────────────────────────────────────────


async def generic_read(
    config: ObjectTypeConfig, params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    """Read an entity by ID or name (pages)."""
    # Page has sub-operations (structure, properties, events, component, event_function)
    if config.has_page_sub_ops:
        from app.agents.appbuilder.tools.crud.page_ops import page_read
        return await page_read(params, context)

    # Application: read by app_code (list UI defs) or by id (full definition)
    if config.object_type == "application":
        return await _read_application(params, context)

    # Standard: GET by ID
    client, headers = _get_client_and_headers(context)
    entity_id = params.get("id")
    if not entity_id:
        return ToolResult(success=False, error="id is required for read.")

    result = await client.get(f"{config.api_path}/{entity_id}", headers=headers)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to read {config.display_name}: {result.error}")

    # Build a compact summary instead of dumping raw JSON
    entity_data = result.data
    summary_parts = [f"{config.display_name}:"]
    if isinstance(entity_data, dict):
        for key in ("name", "appCode", "clientCode", "version", "namespace"):
            if key in entity_data:
                summary_parts.append(f"  {key}: {entity_data[key]}")
        if "definition" in entity_data and isinstance(entity_data["definition"], dict):
            defn_keys = list(entity_data["definition"].keys())[:20]
            summary_parts.append(f"  definition keys: {defn_keys}")
        if "variables" in entity_data and isinstance(entity_data["variables"], dict):
            summary_parts.append(f"  variable breakpoints: {list(entity_data['variables'].keys())}")

    return ToolResult(
        success=True,
        data=entity_data,
        summary="\n".join(summary_parts),
    )


async def _read_application(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Application read: by app_code lists UI defs, by id reads full definition."""
    client, headers = _get_client_and_headers(context)

    entity_id = params.get("id")
    app_code = params.get("app_code") or params.get("name", "")

    if entity_id:
        # Read full UI application definition by ID
        result = await client.get(f"/api/ui/applications/{entity_id}", headers=headers)
        if not result.success:
            return ToolResult(success=False, error=f"Failed to read application: {result.error}")

        app_data = result.data
        code = app_data.get("appCode", "?") if isinstance(app_data, dict) else "?"
        parts = [f"Application '{code}':"]
        if isinstance(app_data, dict):
            for key in ("name", "appCode", "clientCode", "version"):
                if key in app_data:
                    parts.append(f"  {key}: {app_data[key]}")
            props = app_data.get("properties", {})
            if isinstance(props, dict):
                parts.append(f"  property keys: {list(props.keys())[:20]}")
        return ToolResult(
            success=True,
            data=app_data,
            summary="\n".join(parts),
        )

    if app_code:
        # List UI application definitions for this appCode
        result = await client.get(
            "/api/ui/applications",
            headers={**headers, "appCode": app_code},
            params={"page": 0, "size": 100, "appCode": app_code},
        )
        if not result.success:
            return ToolResult(success=False, error=f"Failed to list UI applications: {result.error}")

        data = result.data
        apps = data.get("content", []) if isinstance(data, dict) else []
        lines = [f"- {a.get('name', '?')} (id={a.get('id', '?')}, v{a.get('version', '?')})" for a in apps]

        return ToolResult(
            success=True,
            data=[{"name": a.get("name"), "id": a.get("id"), "appCode": a.get("appCode"), "version": a.get("version")} for a in apps],
            summary=f"Found {len(apps)} UI application definition(s) for appCode '{app_code}':\n" + "\n".join(lines),
        )

    return ToolResult(success=False, error="Provide either 'id' (UI app ID) or 'app_code' to read application.")


# ── UPDATE ────────────────────────────────────────────────────────


async def generic_update(
    config: ObjectTypeConfig, params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    """Update an entity."""
    # Page has sub-operations
    if config.has_page_sub_ops:
        from app.agents.appbuilder.tools.crud.page_ops import page_update
        return await page_update(params, context)

    # Application update
    if config.object_type == "application":
        return await _update_application(params, context)

    # Theme requires confirmation
    if config.requires_confirmation:
        if not params.get("confirmed"):
            return ToolResult(
                success=False,
                error=(
                    "Theme updates affect the entire application. "
                    "You MUST describe the planned changes to the user first, "
                    "then call with confirmed=true after they explicitly agree."
                ),
            )

    from app.agents.appbuilder.tools._shared import save_entity

    client, headers = _get_client_and_headers(context)
    entity_id = params.get("id")
    if not entity_id:
        return ToolResult(success=False, error="id is required for update.")

    # Fetch current
    current = await client.get(f"{config.api_path}/{entity_id}", headers=headers)
    if not current.success:
        return ToolResult(success=False, error=f"Failed to read {config.display_name}: {current.error}")

    entity_data = current.data

    # Apply updates based on config
    if config.has_variables:
        # Theme: merge variables by breakpoint
        variables = params.get("variables", {})
        entity_data.setdefault("variables", {}).update(variables)
    elif config.object_type == "style":
        # Style: partial merge of definition
        definition = params.get("definition", {})
        entity_data.setdefault("definition", {}).update(definition)
    else:
        # Standard: replace definition if provided
        if params.get("definition"):
            entity_data["definition"] = params["definition"]

    if params.get("name"):
        entity_data["name"] = params["name"]
    if params.get("title"):
        entity_data["title"] = params["title"]
    if params.get("description"):
        entity_data["description"] = params["description"]
    entity_data["message"] = params["message"]

    result = await save_entity(
        client, config.api_path, entity_id, entity_data, headers,
        context.get("client_code", ""),
    )
    if not result.success:
        return result

    return ToolResult(success=True, summary=f"Updated {config.display_name} (id={entity_id}).")


async def _update_application(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Update application via the UI service."""
    from app.agents.appbuilder.tools._shared import save_entity

    client, headers = _get_client_and_headers(context)
    application_id = params.get("id")
    if not application_id:
        return ToolResult(success=False, error="id is required for application update.")

    current = await client.get(f"/api/ui/applications/{application_id}", headers=headers)
    if not current.success:
        return ToolResult(success=False, error=f"Failed to read application: {current.error}")

    app_data = current.data
    if params.get("properties"):
        app_data.setdefault("properties", {}).update(params["properties"])
    if params.get("title"):
        app_data["title"] = params["title"]
    if params.get("description"):
        app_data["description"] = params["description"]
    app_data["message"] = params["message"]

    result = await save_entity(
        client, "/api/ui/applications", application_id, app_data, headers,
        context.get("client_code", ""),
    )
    if not result.success:
        return result

    return ToolResult(success=True, summary=f"Updated application (id={application_id}).")


# ── DELETE ────────────────────────────────────────────────────────


async def generic_delete(
    config: ObjectTypeConfig, params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    """Delete an entity."""
    client, headers = _get_client_and_headers(context)

    # Application deletes by app_code via Multi service
    if config.object_type == "application":
        app_code = params.get("app_code")
        if not app_code:
            return ToolResult(success=False, error="app_code is required to delete an application.")

        result = await client.delete(f"/api/multi/application/{app_code}", headers=headers)
        if not result.success:
            return ToolResult(success=False, error=f"Failed to delete application: {result.error}")

        return ToolResult(success=True, summary=f"Deleted application '{app_code}'.")

    # Standard: DELETE by ID
    entity_id = params.get("id")
    if not entity_id:
        return ToolResult(success=False, error="id is required for delete.")

    api_path = config.delete_api_path or config.api_path
    result = await client.delete(f"{api_path}/{entity_id}", headers=headers)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to delete {config.display_name}: {result.error}")

    return ToolResult(success=True, summary=f"Deleted {config.display_name} (id={entity_id}).")
