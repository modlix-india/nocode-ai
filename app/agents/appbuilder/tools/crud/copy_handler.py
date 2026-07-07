"""Copy tool - duplicate definitions across or within applications.

Supports three modes:
1. Copy a whole definition (page, style, theme, function, etc.) to another app
2. Copy a page component subtree into an existing page
3. Copy an entire application (creates new app + copies all child definitions)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.appbuilder.tools.crud._registry import OBJECT_TYPES, OBJECT_TYPE_ENUM

logger = logging.getLogger(__name__)

METADATA_KEYS = {"id", "createdAt", "createdBy", "updatedAt", "updatedBy", "version", "_id"}

# Definition types to copy when cloning an entire application.
_APP_CHILD_TYPES = [
    "page", "style", "theme", "function", "schema",
    "connection", "workflow", "template", "uripath",
]


def _strip_metadata(data: dict[str, Any]) -> dict[str, Any]:
    """Remove server-managed fields so the object can be created fresh."""
    return {k: v for k, v in data.items() if k not in METADATA_KEYS}


def _get_client_and_headers(context: dict[str, Any]) -> tuple:
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context["headers"]


# ── Fetch helpers ────────────────────────────────────────────────


async def _fetch_by_name(
    client: Any, config: Any, name: str, app_code: str, headers: dict[str, str],
) -> tuple[dict[str, Any] | None, str | None]:
    """Fetch a non-page definition by name and appCode (list then full read)."""
    result = await client.get(
        config.api_path,
        headers=headers,
        params={"page": 0, "size": 1, "appCode": app_code, "name": name},
    )
    if not result.success:
        return None, f"Failed to list {config.display_name}: {result.error}"

    data = result.data
    items = data.get("content", []) if isinstance(data, dict) else []
    if not items:
        return None, f"{config.display_name} '{name}' not found in app '{app_code}'."

    entity_id = items[0].get("id")
    full = await client.get(f"{config.api_path}/{entity_id}", headers=headers)
    if not full.success:
        return None, f"Failed to read {config.display_name}: {full.error}"

    return full.data, None


# ── Subtree extraction ───────────────────────────────────────────


def _collect_descendants(comp_def: dict[str, Any], key: str, result: set[str]) -> None:
    """Recursively collect all descendant component keys."""
    comp = comp_def.get(key, {})
    for child_key, active in comp.get("children", {}).items():
        if active and child_key in comp_def:
            result.add(child_key)
            _collect_descendants(comp_def, child_key, result)


def _extract_subtree(
    page_data: dict[str, Any], root_key: str, include_events: bool = True,
) -> tuple[dict[str, Any] | None, str | None]:
    """Extract a component subtree and referenced event functions.

    Returns (subtree_dict, error_string).
    subtree_dict has keys: components, event_functions, root_key.
    """
    comp_def = page_data.get("componentDefinition", {})
    if root_key not in comp_def:
        return None, f"Component '{root_key}' not found in source page."

    keys: set[str] = {root_key}
    _collect_descendants(comp_def, root_key, keys)
    components = {k: comp_def[k] for k in keys if k in comp_def}

    referenced_events: dict[str, Any] = {}
    if include_events:
        event_fns = page_data.get("eventFunctions", {})
        for fn_name, fn_def in event_fns.items():
            for key in keys:
                if key in fn_name:
                    referenced_events[fn_name] = fn_def
                    break

    return {
        "components": components,
        "event_functions": referenced_events,
        "root_key": root_key,
    }, None


def _resolve_key_conflicts(
    source_components: dict[str, Any], target_components: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Rename conflicting component keys.

    Returns (renamed_components, rename_map).
    """
    all_existing = set(target_components.keys())
    rename_map: dict[str, str] = {}

    for key in source_components:
        if key in all_existing:
            new_key = key + "_copy"
            counter = 1
            while new_key in all_existing or new_key in source_components:
                new_key = f"{key}_copy{counter}"
                counter += 1
            rename_map[key] = new_key
            all_existing.add(new_key)

    if not rename_map:
        return source_components, rename_map

    renamed: dict[str, Any] = {}
    for old_key, comp in source_components.items():
        new_key = rename_map.get(old_key, old_key)
        comp = {**comp, "key": new_key, "name": new_key}
        if "children" in comp:
            comp["children"] = {
                rename_map.get(ck, ck): v
                for ck, v in comp["children"].items()
            }
        renamed[new_key] = comp

    return renamed, rename_map


def _rename_event_functions(
    events: dict[str, Any], rename_map: dict[str, str],
) -> dict[str, Any]:
    """Apply key renames to event function names."""
    if not rename_map:
        return events
    renamed: dict[str, Any] = {}
    for fn_name, fn_def in events.items():
        new_name = fn_name
        for old_key, new_key in rename_map.items():
            new_name = new_name.replace(old_key, new_key)
        renamed[new_name] = fn_def
    return renamed


# ── Copy modes ───────────────────────────────────────────────────


async def _copy_page_subtree(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Copy a component subtree from one page into another."""
    from app.agents.appbuilder.tools._executor import fetch_page_by_name, save_page

    client, headers = _get_client_and_headers(context)

    source_app = params["source_app_code"]
    source_name = params["source_name"]
    source_key = params["source_component_key"]
    target_app = params["target_app_code"]
    target_page = params.get("target_page_name", "")
    target_parent = params.get("target_parent_key", "")
    include_events = params.get("include_events", True)

    if not target_page:
        return ToolResult(success=False, error="target_page_name is required for subtree copy.")
    if not target_parent:
        return ToolResult(success=False, error="target_parent_key is required for subtree copy.")

    # Fetch source page
    src_data, err = await fetch_page_by_name(client, source_name, source_app, headers)
    if err:
        return ToolResult(success=False, error=f"Source page: {err}")

    # Extract subtree
    subtree, err = _extract_subtree(src_data, source_key, include_events)
    if err:
        return ToolResult(success=False, error=err)

    # Fetch target page
    tgt_data, err = await fetch_page_by_name(client, target_page, target_app, headers)
    if err:
        return ToolResult(success=False, error=f"Target page: {err}")

    tgt_comp_def = tgt_data.setdefault("componentDefinition", {})
    if target_parent not in tgt_comp_def:
        return ToolResult(
            success=False,
            error=f"Target parent '{target_parent}' not found in page '{target_page}'.",
        )

    # Resolve key conflicts
    components, rename_map = _resolve_key_conflicts(subtree["components"], tgt_comp_def)
    events = _rename_event_functions(subtree["event_functions"], rename_map)

    # Merge components into target
    tgt_comp_def.update(components)

    # Register the subtree root as a child of the target parent
    root_key = rename_map.get(subtree["root_key"], subtree["root_key"])
    tgt_comp_def[target_parent].setdefault("children", {})[root_key] = True

    # Merge event functions
    if events:
        tgt_data.setdefault("eventFunctions", {}).update(events)

    tgt_data["message"] = params["message"]
    save_result = await save_page(
        client, tgt_data["id"], tgt_data, headers, context.get("client_code", ""),
    )
    if not save_result.success:
        return save_result

    renamed_info = f" (renamed: {rename_map})" if rename_map else ""
    return ToolResult(
        success=True,
        summary=(
            f"Copied subtree '{source_key}' ({len(components)} components, "
            f"{len(events)} events) from '{source_name}' into '{target_page}' "
            f"under '{target_parent}'{renamed_info}."
        ),
    )


async def _copy_page(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Copy an entire page to a target app with a new name."""
    from app.agents.appbuilder.tools._executor import fetch_page_by_name

    client, headers = _get_client_and_headers(context)

    source_app = params["source_app_code"]
    source_name = params["source_name"]
    target_app = params["target_app_code"]
    target_name = params.get("target_name") or source_name

    if source_app == target_app and target_name == source_name:
        return ToolResult(
            success=False,
            error="target_name must differ from source_name when copying within the same app.",
        )

    src_data, err = await fetch_page_by_name(client, source_name, source_app, headers)
    if err:
        return ToolResult(success=False, error=f"Source page: {err}")

    page_data = _strip_metadata(src_data)
    page_data["name"] = target_name
    page_data["appCode"] = target_app
    page_data["clientCode"] = context.get("client_code", "")
    page_data["message"] = params["message"]

    result = await client.post("/api/ui/pages", headers=headers, json=page_data)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to create page: {result.error}")

    created = result.data
    page_id = created.get("id", "?") if isinstance(created, dict) else "?"
    comp_count = len(page_data.get("componentDefinition", {}))
    event_count = len(page_data.get("eventFunctions", {}))
    return ToolResult(
        success=True,
        summary=(
            f"Copied page '{source_name}' → '{target_name}' in app '{target_app}' "
            f"(id={page_id}, {comp_count} components, {event_count} events)."
        ),
    )


async def _copy_definition(
    config: Any, params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    """Copy a non-page, non-application definition."""
    client, headers = _get_client_and_headers(context)

    source_app = params["source_app_code"]
    source_name = params["source_name"]
    target_app = params["target_app_code"]
    target_name = params.get("target_name") or source_name

    if source_app == target_app and target_name == source_name:
        return ToolResult(
            success=False,
            error="target_name must differ from source_name when copying within the same app.",
        )

    src_data, err = await _fetch_by_name(client, config, source_name, source_app, headers)
    if err:
        return ToolResult(success=False, error=err)

    body = _strip_metadata(src_data)
    body["name"] = target_name
    body["appCode"] = target_app
    body["clientCode"] = context.get("client_code", "")
    body["message"] = params["message"]

    api_path = config.create_api_path or config.api_path
    result = await client.post(api_path, headers=headers, json=body)
    if not result.success:
        return ToolResult(
            success=False,
            error=f"Failed to create {config.display_name}: {result.error}",
        )

    created = result.data
    entity_id = created.get("id", "?") if isinstance(created, dict) else "?"
    return ToolResult(
        success=True,
        summary=(
            f"Copied {config.display_name} '{source_name}' → '{target_name}' "
            f"in app '{target_app}' (id={entity_id})."
        ),
    )


async def _copy_application(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Clone an application: create new app, then copy all child definitions."""
    client, headers = _get_client_and_headers(context)

    source_app = params["source_app_code"]
    target_app = params["target_app_code"]
    target_name = params.get("target_name") or target_app

    if target_name != target_app:
        return ToolResult(
            success=False,
            error=f"Application name must equal appCode. Got name='{target_name}', appCode='{target_app}'.",
        )

    # Create the new application
    app_type = params.get("app_type", "APP")
    create_body: dict[str, Any] = {
        "appName": target_name,
        "appCode": target_app,
        "appType": app_type,
        "message": params["message"],
    }
    result = await client.post("/api/multi/application", headers=headers, json=create_body)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to create application: {result.error}")

    # Copy each definition type from source → target
    copied: list[str] = []
    errors: list[str] = []

    for type_name in _APP_CHILD_TYPES:
        config = OBJECT_TYPES.get(type_name)
        if not config:
            continue

        # List definitions in source app
        list_result = await client.get(
            config.api_path,
            headers=headers,
            params={"page": 0, "size": 1000, "appCode": source_app},
        )
        if not list_result.success:
            errors.append(f"{type_name}: list failed")
            continue

        data = list_result.data
        items = data.get("content", []) if isinstance(data, dict) else []
        if not items:
            continue

        for item in items:
            entity_id = item.get("id")
            entity_name = item.get("name", "?")

            # Fetch full definition
            full = await client.get(f"{config.api_path}/{entity_id}", headers=headers)
            if not full.success:
                errors.append(f"{type_name} '{entity_name}': read failed")
                continue

            body = _strip_metadata(full.data)
            body["name"] = entity_name
            body["appCode"] = target_app
            body["clientCode"] = context.get("client_code", "")
            body["message"] = params["message"]

            api_path = config.create_api_path or config.api_path
            create_result = await client.post(api_path, headers=headers, json=body)
            if create_result.success:
                copied.append(f"{type_name}: {entity_name}")
            else:
                errors.append(f"{type_name} '{entity_name}': {create_result.error}")

    # Track in session context
    session_ctx = context.get("session_context")
    if session_ctx is not None:
        session_ctx["app_code"] = target_app
        app_codes = session_ctx.setdefault("app_codes", [])
        if target_app not in app_codes:
            app_codes.append(target_app)

    parts = [f"Created application '{target_app}'. Copied {len(copied)} definition(s)."]
    if errors:
        parts.append(f"{len(errors)} error(s): {'; '.join(errors[:5])}")
    return ToolResult(success=True, summary=" ".join(parts))


# ── Main dispatch ────────────────────────────────────────────────


async def _copy_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Route to the appropriate copy mode."""
    object_type = params.get("object_type", "")
    config = OBJECT_TYPES.get(object_type)
    if not config:
        return ToolResult(success=False, error=f"Unknown object_type: {object_type}")

    source_app = params.get("source_app_code", "")
    if not source_app:
        return ToolResult(success=False, error="source_app_code is required.")

    target_app = params.get("target_app_code", "")
    if not target_app:
        return ToolResult(success=False, error="target_app_code is required.")

    # Mode 3: Copy application
    if object_type == "application":
        return await _copy_application(params, context)

    source_name = params.get("source_name", "")
    if not source_name:
        return ToolResult(success=False, error="source_name is required.")

    # Mode 2: Copy page subtree
    if object_type == "page" and params.get("source_component_key"):
        return await _copy_page_subtree(params, context)

    # Mode 1: Copy whole definition
    if object_type == "page":
        return await _copy_page(params, context)

    return await _copy_definition(config, params, context)


# ── Tool definition ──────────────────────────────────────────────


copy_tool = ToolDefinition(
    name="copy",
    display_name="Copy",
    # v8 Plan B WS4 · declarative only · blocking elicitation (request_confirmation).
    kind="elicitation",
    elicit_mode="blocking",
    description=(
        "Copy a definition object to another application or within the same application "
        "with a new name. Supports copying entire pages, specific component subtrees "
        "(with their event functions) into existing pages, and cloning entire applications "
        "with all their definitions.\n\n"
        "Modes:\n"
        "1. Copy whole definition: provide source_app_code, source_name, target_app_code, "
        "   and optionally target_name.\n"
        "2. Copy page subtree: also provide source_component_key, target_page_name, "
        "   and target_parent_key to copy a portion of a page into an existing page.\n"
        "3. Copy application: set object_type='application' to clone an entire app "
        "   with all child definitions (pages, styles, themes, functions, etc.).\n\n"
        "Note: Application name must always equal appCode."
    ),
    parameters=[
        ToolParameter(
            name="object_type",
            type="string",
            description="Type of definition to copy.",
            required=True,
            enum=OBJECT_TYPE_ENUM,
        ),
        ToolParameter(
            name="source_app_code",
            type="string",
            description="Application code of the source definition.",
            required=True,
        ),
        ToolParameter(
            name="source_name",
            type="string",
            description="Name of the source definition (not required for application type).",
            required=False,
        ),
        ToolParameter(
            name="target_app_code",
            type="string",
            description="Application code to copy into.",
            required=True,
        ),
        ToolParameter(
            name="target_name",
            type="string",
            description="New name for the copied definition. Defaults to source_name. Required when copying within the same app.",
            required=False,
        ),
        ToolParameter(
            name="message",
            type="string",
            description="Commit message (10-15 words describing the copy operation).",
            required=True,
        ),
        ToolParameter(
            name="source_component_key",
            type="string",
            description="For page subtree copy: the component key to use as the root of the subtree to copy.",
            required=False,
        ),
        ToolParameter(
            name="target_page_name",
            type="string",
            description="For page subtree copy: the destination page name.",
            required=False,
        ),
        ToolParameter(
            name="target_parent_key",
            type="string",
            description="For page subtree copy: the parent component key in the target page to insert under.",
            required=False,
        ),
        ToolParameter(
            name="include_events",
            type="boolean",
            description="For page subtree copy: whether to also copy referenced event functions. Default: true.",
            required=False,
            default=True,
        ),
    ],
    execute=_copy_execute,
)
