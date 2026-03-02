"""Page management tools — list, create, delete, read structure.

These tools let the agent manage pages within an application.
The agent never sees the full 30K+ page JSON — the executor
builds compact summaries and trees.
"""

from __future__ import annotations

from typing import Any

from app.core.tools.base import ToolDefinition, ToolResult, ToolParameter
from app.core.tools.http_client import SaasClient
from app.agents.appbuilder.tools._executor import (
    fetch_page_by_name,
    fetch_page_by_id,
    save_page,
    build_component_tree,
    summarize_component,
)

API_PREFIX = "/api/ui/pages"


def _get_client_and_headers(context: dict[str, Any]) -> tuple[SaasClient, dict[str, str]]:
    """Extract SaasClient and headers from tool context."""
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context["headers"]


# ── list_pages ──────────────────────────────────────────────────

async def _list_pages_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    from app.agents.appbuilder.tools._shared import require_app_code

    client, headers = _get_client_and_headers(context)
    app_code = params.get("app_code") or context.get("app_code", "")
    if not app_code:
        _, err = require_app_code(context)
        return err

    result = await client.get(
        API_PREFIX,
        headers=headers,
        params={"page": 0, "size": 1000, "appCode": app_code},
    )

    if not result.success:
        return ToolResult(success=False, error=f"Failed to list pages: {result.error}")

    data = result.data
    pages = data.get("content", []) if isinstance(data, dict) else []

    summary_lines = []
    for page in pages:
        comp_def = page.get("componentDefinition", {})
        name = page.get("name", "?")
        page_id = page.get("id", "?")
        root = page.get("rootComponent", "")
        comp_count = len(comp_def)
        version = page.get("version", "?")
        summary_lines.append(f"- {name} (id={page_id}, root={root}, components={comp_count}, v{version})")

    summary = f"Found {len(pages)} pages:\n" + "\n".join(summary_lines)
    return ToolResult(success=True, data={"pages": [{"name": p.get("name"), "id": p.get("id"), "version": p.get("version")} for p in pages]}, summary=summary)


list_pages = ToolDefinition(
    name="list_pages",
    display_name="List Pages",
    description="List all pages in an application. Returns page names, IDs, and component counts.",
    parameters=[
        ToolParameter(name="app_code", type="string", description="Application code to list pages for.", required=False),
    ],
    execute=_list_pages_execute,
)


# ── create_page ─────────────────────────────────────────────────

async def _create_page_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    from app.agents.appbuilder.tools._shared import validate_name, require_app_code

    client, headers = _get_client_and_headers(context)
    app_code = params.get("app_code") or context.get("app_code", "")
    if not app_code:
        _, err = require_app_code(context)
        return err
    page_name = params["page_name"]

    err = validate_name(page_name)
    if err:
        return err

    title = params.get("title", page_name)

    # Create page with a root Grid component
    root_key = "root"
    page_data: dict[str, Any] = {
        "name": page_name,
        "appCode": app_code,
        "clientCode": context.get("client_code", ""),
        "title": title,
        "rootComponent": root_key,
        "componentDefinition": {
            root_key: {
                "key": root_key,
                "type": "Grid",
                "name": root_key,
                "displayOrder": 0,
                "children": {},
                "properties": {},
                "styleProperties": {},
            }
        },
        "eventFunctions": {},
        "properties": {},
        "translations": {},
    }
    if params.get("description"):
        page_data["description"] = params["description"]
    page_data["message"] = params["message"]

    result = await client.post(API_PREFIX, headers=headers, json=page_data)

    if not result.success:
        return ToolResult(success=False, error=f"Failed to create page: {result.error}")

    created = result.data
    page_id = created.get("id", "?") if isinstance(created, dict) else "?"

    return ToolResult(
        success=True,
        data={"id": page_id, "name": page_name},
        summary=f"Created page '{page_name}' (id={page_id}) with root Grid component.",
    )


create_page = ToolDefinition(
    name="create_page",
    display_name="Create Page",
    description="Create a new page with an empty root Grid layout. Returns the page ID.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Name for the new page (letters only, e.g. 'loginPage', 'dashboard')."),
        ToolParameter(name="title", type="string", description="Human-readable page title.", required=False),
        ToolParameter(name="description", type="string", description="Page description.", required=False),
        ToolParameter(name="message", type="string", description="Commit message (10–15 words) describing what was changed."),
        ToolParameter(name="app_code", type="string", description="Application code. Uses session app if not specified.", required=False),
    ],
    execute=_create_page_execute,
)


# ── delete_page ─────────────────────────────────────────────────

async def _delete_page_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    page_id = params["page_id"]

    result = await client.delete(f"{API_PREFIX}/{page_id}", headers=headers)

    if not result.success:
        return ToolResult(success=False, error=f"Failed to delete page: {result.error}")

    return ToolResult(success=True, summary=f"Deleted page (id={page_id}).")


delete_page = ToolDefinition(
    name="delete_page",
    display_name="Delete Page",
    description="Delete a page by its ID. If the page is inherited (owned by another client), this removes your override and resets to the inherited version.",
    parameters=[
        ToolParameter(name="page_id", type="string", description="The page ID to delete."),
    ],
    execute=_delete_page_execute,
)


# ── read_page_structure ─────────────────────────────────────────

async def _read_page_structure_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    from app.agents.appbuilder.tools._shared import require_app_code

    client, headers = _get_client_and_headers(context)
    page_name = params["page_name"]
    app_code = params.get("app_code") or context.get("app_code", "")
    if not app_code:
        _, err = require_app_code(context)
        return err

    page_data, error = await fetch_page_by_name(client, page_name, app_code, headers)
    if error:
        return ToolResult(success=False, error=error)

    tree = build_component_tree(page_data)
    comp_count = len(page_data.get("componentDefinition", {}))
    event_count = len(page_data.get("eventFunctions", {}))

    summary = (
        f"Page '{page_name}' structure ({comp_count} components, {event_count} event functions):\n\n"
        f"{tree}"
    )

    return ToolResult(success=True, summary=summary)


read_page_structure = ToolDefinition(
    name="read_page_structure",
    display_name="Read Page Structure",
    description=(
        "Read a page's component tree structure. Returns a human-readable tree "
        "showing component keys, types, and hierarchy. Does NOT return full properties — "
        "use read_component for that."
    ),
    parameters=[
        ToolParameter(name="page_name", type="string", description="Name of the page to read."),
        ToolParameter(name="app_code", type="string", description="Application code. Uses session app if not specified.", required=False),
    ],
    execute=_read_page_structure_execute,
)


# ── read_page_properties ────────────────────────────────────────

async def _read_page_properties_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    from app.agents.appbuilder.tools._shared import require_app_code

    client, headers = _get_client_and_headers(context)
    page_name = params["page_name"]
    app_code = params.get("app_code") or context.get("app_code", "")
    if not app_code:
        _, err = require_app_code(context)
        return err

    page_data, error = await fetch_page_by_name(client, page_name, app_code, headers)
    if error:
        return ToolResult(success=False, error=error)

    import json
    page_props = page_data.get("properties", {})
    props = {
        "id": page_data.get("id"),
        "name": page_data.get("name"),
        "description": page_data.get("description"),
        "rootComponent": page_data.get("rootComponent"),
        "properties": page_props,
        "translations": page_data.get("translations", {}),
        "permission": page_data.get("permission"),
        "version": page_data.get("version"),
    }
    # Surface the page title from properties.title.name for clarity
    title_obj = page_props.get("title", {})
    title_name = title_obj.get("name", {})
    if isinstance(title_name, dict):
        props["_pageTitle"] = title_name.get("value") or title_name.get("location", {}).get("expression", "")


    return ToolResult(
        success=True,
        data=props,
        summary=f"Page '{page_name}' properties:\n{json.dumps(props, indent=2, default=str)}",
    )


read_page_properties = ToolDefinition(
    name="read_page_properties",
    display_name="Read Page Properties",
    description="Read page-level properties (title, description, translations, permissions, version). Does NOT read components — use read_page_structure for that.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Name of the page."),
        ToolParameter(name="app_code", type="string", description="Application code.", required=False),
    ],
    execute=_read_page_properties_execute,
)


# ── update_page_properties ──────────────────────────────────────

async def _update_page_properties_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    from app.agents.appbuilder.tools._shared import require_app_code

    client, headers = _get_client_and_headers(context)
    page_name = params["page_name"]
    app_code = params.get("app_code") or context.get("app_code", "")
    if not app_code:
        _, err = require_app_code(context)
        return err
    updates = params.get("properties", {})

    page_data, error = await fetch_page_by_name(client, page_name, app_code, headers)
    if error:
        return ToolResult(success=False, error=error)

    # Merge updates into page data
    for key, value in updates.items():
        if key == "title":
            # Page title lives in properties.title.name — NOT the top-level "title" field.
            # Accepts: a plain string (wrapped as {value: str}), or a full location object.
            page_props = page_data.setdefault("properties", {})
            title_obj = page_props.setdefault("title", {})
            if isinstance(value, str):
                title_obj["name"] = {"value": value}
            elif isinstance(value, dict):
                # Allow passing the full title object (e.g. {name: {value: "..."}, append: {value: false}})
                title_obj.update(value)
        elif key in ("permission", "description"):
            page_data[key] = value
        elif key == "translations":
            page_data.setdefault("translations", {}).update(value)
        elif key == "properties":
            page_data.setdefault("properties", {}).update(value)

    page_data["message"] = params["message"]

    save_result = await save_page(client, page_data["id"], page_data, headers, context.get("client_code", ""))
    if not save_result.success:
        return save_result

    return ToolResult(
        success=True,
        summary=f"Updated page '{page_name}' properties: {list(updates.keys())}",
    )


update_page_properties = ToolDefinition(
    name="update_page_properties",
    display_name="Update Page Properties",
    description=(
        "Update page-level properties. For title: pass a string (e.g. {\"title\": \"My Page\"}) "
        "or a full object (e.g. {\"title\": {\"name\": {\"value\": \"My Page\"}, \"append\": {\"value\": false}}}). "
        "The title is stored in properties.title.name, NOT the top-level title field."
    ),
    parameters=[
        ToolParameter(name="page_name", type="string", description="Name of the page to update."),
        ToolParameter(
            name="properties",
            type="object",
            description=(
                "Properties to update. Keys: title (string or {name: {value: str}, append: {value: bool}}), "
                "permission, description, translations, properties (nested page properties like onLoadEvent, seo, classes)."
            ),
        ),
        ToolParameter(name="message", type="string", description="Commit message (10–15 words) describing what was changed."),
        ToolParameter(name="app_code", type="string", description="Application code.", required=False),
    ],
    execute=_update_page_properties_execute,
)


# ── Export all page tools ───────────────────────────────────────

PAGE_TOOLS: list[ToolDefinition] = [
    list_pages,
    create_page,
    delete_page,
    read_page_structure,
    read_page_properties,
    update_page_properties,
]
