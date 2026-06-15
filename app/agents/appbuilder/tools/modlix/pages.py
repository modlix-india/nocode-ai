"""Page CRUD + composition tools.

Ports modlix-mcp/modlix_mcp/tools/{pages,composition,composition_v2}.py
into a single nocode-ai module — 26 tools total:

Page CRUD (reads + writes on the page envelope):
  - list_pages
  - get_page              (tree/properties/events/full views)
  - create_page
  - update_page
  - reset_page_composition  (DESTRUCTIVE)
  - replace_page_definition (DESTRUCTIVE)
  - delete_page             (DESTRUCTIVE)
  - get_page_summary        (safe on huge pages)
  - get_component_subtree   (bounded read)
  - search_page_components  (per-page filter)
  - search_pages            (cross-page substring scan)
  - get_component           (one component's summary)
  - get_component_styles    (styleProperties leaves)

Composition (in-place page mutations):
  - add_component
  - update_component_props
  - set_styles            (merge|replace mode)
  - delete_style_rule
  - set_bindings
  - move_component
  - remove_component
  - rename_component
  - bulk_patch_component_props

Composition v2 (surgical PATCH variants — one component per call):
  - patch_component_props
  - patch_component_bindings
  - patch_component_styles
  - remove_component_styles

Auth: every tool reads JWT via `context["headers"]`; no separate dev login.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

from . import _conventions as c
from . import _page_ops as p_ops


# ── Helpers ──────────────────────────────────────────────────────────────


def _client_and_headers(context: dict[str, Any]) -> tuple[Any, dict[str, str]]:
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context.get("headers") or {}


def _resolve_app_code(params: dict[str, Any], context: dict[str, Any]) -> tuple[str, ToolResult | None]:
    """Return (app_code, error)."""
    ac = params.get("app_code") or context.get("app_code", "")
    if not ac:
        return "", ToolResult(
            success=False,
            error="No appCode set. Pass `app_code` or set it on the chat request.",
        )
    return ac, None


def _resolve_client_code(params: dict[str, Any], context: dict[str, Any]) -> str:
    return params.get("client_code") or context.get("client_code", "") or ""


def _validate_simple_name(name: str) -> str | None:
    """Match modlix-mcp's _shared.validate_name semantics for page names."""
    if not name:
        return "Name must not be empty"
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9]*$", name):
        return f"Invalid page name '{name}': letters/digits only, must start with a letter"
    return None


def _validate_properties(component_type: str, properties: dict[str, Any] | None) -> str | None:
    """Catalog-aware prop validation — error message or None.

    Looks up the component's catalog entry; for any property NOT in the
    catalog's known list AND not a known platform-safe prop, returns an
    error. Returns None when catalog is empty (fallback / unloaded) — better
    to let the platform validate than block on a missing catalog.
    """
    if not properties:
        return None
    from app.agents.appbuilder.catalog import get_catalog
    info = get_catalog().get_component_info(component_type) or {}
    known: set[str] = set()
    for pn in (info.get("properties") or []):
        if isinstance(pn, dict) and pn.get("name"):
            known.add(pn["name"])
    if not known:
        return None
    # Platform-safe always-allowed prop names that aren't in every component's catalog entry.
    PLATFORM_SAFE = {
        "visibility", "designType", "colorScheme", "bgColor", "background",
        "onClick", "onSubmit", "onChange", "onBlur", "onFocus", "onLoad",
        "linkPath", "pathsActiveFor", "label", "name", "key",
    }
    unknown = [k for k in properties.keys() if k not in known and k not in PLATFORM_SAFE]
    if unknown:
        return (
            f"Unknown properties for '{component_type}': {unknown}. "
            f"Valid (catalog): {sorted(known)[:20]}"
            + ("..." if len(known) > 20 else "")
        )
    return None


async def _load_save(
    page_name: str,
    context: dict[str, Any],
    params: dict[str, Any],
    mutate: Any,
    message: str,
) -> tuple[bool, str]:
    """Helper: load page, run mutate(page) → error|None, save. Returns (success, error)."""
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return False, err_result.error
    client, headers = _client_and_headers(context)
    page, err = await p_ops.fetch_page_by_name(client, page_name, ac, headers)
    if err:
        return False, err
    assert page is not None
    err = mutate(page)
    if err:
        return False, err
    save = await p_ops.save_page(client, page, headers, _resolve_client_code(params, context), message=message)
    if not save.success:
        return False, save.error
    return True, ""


# ── list_pages ───────────────────────────────────────────────────────────


async def _execute_list_pages(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    try:
        size = max(1, min(int(params.get("size") or 200), 1000))
    except (TypeError, ValueError):
        size = 200
    client, headers = _client_and_headers(context)
    r = await client.get(p_ops.API_PREFIX, headers=headers, params={"page": 0, "size": size, "appCode": ac})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    rows = [{"name": x.get("name"), "id": x.get("id"), "version": x.get("version"), "clientCode": x.get("clientCode")} for x in content]
    return ToolResult(success=True, summary=f"Pages in app '{ac}' ({len(rows)}):\n{json.dumps(rows, indent=2, default=str)}")


list_pages_tool = ToolDefinition(
    name="list_pages",
    description="List pages in an application with their ids and versions.",
    parameters=[
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
        ToolParameter(name="size", type="integer", required=False, default=200, description="Max pages (capped at 1000)"),
    ],
    execute=_execute_list_pages,
)


# ── get_page ─────────────────────────────────────────────────────────────


async def _execute_get_page(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    include = (params.get("include") or "tree").strip()
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    page, err = await p_ops.fetch_page_by_name(client, name, ac, headers)
    if err:
        return ToolResult(success=False, error=err)
    assert page is not None

    if include == "full":
        return ToolResult(success=True, summary=json.dumps(page, indent=2, default=str))
    if include == "properties":
        return ToolResult(success=True, summary=json.dumps({
            "id": page.get("id"),
            "name": page.get("name"),
            "rootComponent": page.get("rootComponent"),
            "properties": page.get("properties", {}),
            "translations": page.get("translations", {}),
            "permission": page.get("permission"),
            "version": page.get("version"),
        }, indent=2, default=str))
    if include == "events":
        events = page.get("eventFunctions") or {}
        if not events:
            return ToolResult(success=True, summary=f"Page '{name}' has no event functions.")
        lines = [f"- {n} ({len((d or {}).get('steps') or {})} steps)" for n, d in events.items()]
        return ToolResult(success=True, summary="\n".join(lines))

    # default: tree
    comp_count = len(page.get("componentDefinition") or {})
    ev_count = len(page.get("eventFunctions") or {})
    tree = p_ops.build_component_tree(page)
    return ToolResult(success=True, summary=f"Page '{name}' ({comp_count} components, {ev_count} event functions):\n\n{tree}")


get_page_tool = ToolDefinition(
    name="get_page",
    description="""Read a page by name. The default `include="tree"` returns the component tree outline with keys + types — fast, cheap, the right choice for navigating to a specific component.

Choose `include` based on what you need:
- `tree` (default) — component tree summary (key, type, parent, name). Use to find which component to edit.
- `properties` — page-level properties (title, layout, permissions). Use when editing page-level config.
- `events` — list of page-level event functions (onLoad, etc.). Use when wiring page lifecycle events.
- `full` — entire page document including every component's full props/styles/bindings. EXPENSIVE on real pages (10-100KB). Avoid unless you need to introspect every component at once.

Typical flow:
1. `get_page(name="contact")` (default tree) — find the component key you need to edit.
2. `get_component(page_name="contact", component_key="emailInput")` — read just that one component if you need its current props before patching.
3. `patch_component_props` / `patch_component_styles` — make the edit.

Don't reach for `include="full"` reflexively — it returns 10× the data of `tree` and you almost never need it. If the agent's task is "modify component X", get the tree, find X's key, then `get_component` X.

For finding components by TYPE or NAME instead of navigating the tree, use `search_page_components` — that's the inverse lookup.""",
    parameters=[
        ToolParameter(name="name", type="string", description="Page name (case-sensitive)"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
        ToolParameter(name="include", type="string", required=False, default="tree", description="tree (default, cheapest) | properties | events | full (expensive — last resort)"),
    ],
    execute=_execute_get_page,
)


# ── create_page ──────────────────────────────────────────────────────────


async def _execute_create_page(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    err = _validate_simple_name(name)
    if err:
        return ToolResult(success=False, error=err)
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    cc = _resolve_client_code(params, context)
    title = params.get("title")
    message = params.get("message") or "Created via CFA"
    body = p_ops.new_page_skeleton(name, ac, cc, title=title)
    body["message"] = message
    client, headers = _client_and_headers(context)
    r = await client.post(p_ops.API_PREFIX, headers=headers, json=body)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    pid = (r.data or {}).get("id", "?") if isinstance(r.data, dict) else "?"
    return ToolResult(success=True, summary=f"Created page '{name}' (id={pid}) with root Grid 'root'.")


create_page_tool = ToolDefinition(
    name="create_page",
    description="""Create a new empty page in the current app. The page is created with a single root Grid container component (key=`root`); you populate it with `add_component` calls afterwards.

Use this as the FIRST call when building a new page from scratch — never try to author the full page in one shot. The flow is:
1. `create_page(name="contact", title="Contact Us")` — empty page exists.
2. `add_component(page_name="contact", parent_key="root", component_key="form", type="Grid", properties={...})` — add layout.
3. `add_component(page_name="contact", parent_key="form", component_key="emailInput", type="TextBox", properties={...})` — add children.
4. … and so on for each component.
5. Optional: `patch_component_styles` for theme-aware styling, `create_page_event_function` + `patch_component_props` to wire onClick handlers.

IMPORTANT — page name rules:
- Letters and digits only. No hyphens, no underscores, no spaces. `contactUs` ✓ , `contact-us` ✗
- Must be unique within the app. If a page with the same name exists, this fails — use a different name or call `update_page` to modify the existing one.
- The name is the URL slug (the path component the user types) AND the internal reference. Keep it short + meaningful.

The optional `title` is the browser tab text. Defaults to the page name if omitted; set it explicitly to a human-readable phrase when the page name is camelCase (`title="Contact Us"` for `name="contactUs"`).""",
    parameters=[
        ToolParameter(name="name", type="string", description="Page name (letters/digits only — used as URL slug + internal key)"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
        ToolParameter(name="client_code", type="string", required=False, description="Owning clientCode"),
        ToolParameter(name="title", type="string", required=False, description="Browser title; defaults to name. Set for camelCase page names ('Contact Us' for 'contactUs')."),
        ToolParameter(name="message", type="string", required=False, description="Commit message"),
    ],
    execute=_execute_create_page,
)


# ── update_page ──────────────────────────────────────────────────────────


async def _execute_update_page(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")

    def mutate(page: dict[str, Any]) -> str | None:
        changed: list[str] = []
        if params.get("title") is not None:
            page.setdefault("properties", {}).setdefault("title", {})["name"] = {"value": params["title"]}
            changed.append("title")
        if params.get("description") is not None:
            page["description"] = params["description"]
            changed.append("description")
        if params.get("properties") is not None:
            page.setdefault("properties", {}).update(params["properties"])
            changed.append("properties")
        if params.get("permission") is not None:
            page["permission"] = params["permission"]
            changed.append("permission")
        if not changed:
            return "No-op: no fields supplied to update_page"
        mutate._changed = changed  # type: ignore[attr-defined]
        return None

    ok, err = await _load_save(name, context, params, mutate, params.get("message") or "Updated via CFA")
    if not ok:
        return ToolResult(success=False, error=err)
    return ToolResult(success=True, summary=f"Updated page '{name}': {', '.join(getattr(mutate, '_changed', []))}")


update_page_tool = ToolDefinition(
    name="update_page",
    description="Update page-level metadata (title, description, properties, permission). For component changes use the composition tools.",
    parameters=[
        ToolParameter(name="name", type="string", description="Page name to update"),
        ToolParameter(name="title", type="string", required=False, description="New browser title"),
        ToolParameter(name="description", type="string", required=False, description="Page description"),
        ToolParameter(name="properties", type="object", required=False, description="Page-level properties to merge"),
        ToolParameter(name="permission", type="string", required=False, description="Required permission to view"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
        ToolParameter(name="message", type="string", required=False, description="Commit message"),
    ],
    execute=_execute_update_page,
)


# ── reset_page_composition ───────────────────────────────────────────────


async def _execute_reset_page_composition(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    root_grid_name = params.get("root_grid_name") or "rootGrid"

    def mutate(page: dict[str, Any]) -> str | None:
        page["rootComponent"] = "root"
        page["componentDefinition"] = {
            "root": {
                "key": "root", "type": "Grid", "name": root_grid_name,
                "displayOrder": 0, "children": {}, "properties": {}, "styleProperties": {},
            }
        }
        page["eventFunctions"] = {}
        return None

    ok, err = await _load_save(name, context, params, mutate, params.get("message") or "Reset composition via CFA")
    if not ok:
        return ToolResult(success=False, error=err)
    return ToolResult(success=True, summary=f"Reset page '{name}' to a single empty Grid 'root'. Title and properties preserved.")


reset_page_composition_tool = ToolDefinition(
    name="reset_page_composition",
    description="Wipe componentDefinition + eventFunctions, keep the page record (id, name, app, client, title, properties). Replaces tree with single empty Grid. DESTRUCTIVE.",
    parameters=[
        ToolParameter(name="name", type="string", description="Page name to reset"),
        ToolParameter(name="root_grid_name", type="string", required=False, default="rootGrid", description="Display name for the new root Grid"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
        ToolParameter(name="message", type="string", required=False, description="Commit message"),
    ],
    execute=_execute_reset_page_composition,
)


# ── replace_page_definition ──────────────────────────────────────────────


async def _execute_replace_page_definition(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    component_definition = params.get("component_definition")
    root_component = (params.get("root_component") or "").strip()
    if not name or not isinstance(component_definition, dict) or not root_component:
        return ToolResult(success=False, error="`name`, `component_definition` (dict), and `root_component` are required")
    if root_component not in component_definition:
        return ToolResult(success=False, error=f"root_component={root_component!r} is not a key in component_definition")
    event_functions = params.get("event_functions")
    properties = params.get("properties")

    def mutate(page: dict[str, Any]) -> str | None:
        page["rootComponent"] = root_component
        page["componentDefinition"] = component_definition
        if event_functions is not None:
            page["eventFunctions"] = event_functions
        if properties is not None:
            page["properties"] = properties
        return None

    ok, err = await _load_save(name, context, params, mutate, params.get("message") or "Replaced page definition via CFA")
    if not ok:
        return ToolResult(success=False, error=err)
    extra = f"; {len(event_functions)} event functions" if event_functions is not None else ""
    return ToolResult(success=True, summary=f"Replaced definition of page '{name}' ({len(component_definition)} components{extra}).")


replace_page_definition_tool = ToolDefinition(
    name="replace_page_definition",
    description="Replace the page's tree + event functions wholesale. Preserves id, name, app, client. DESTRUCTIVE — for surgical edits prefer add_component / patch_component_*.",
    parameters=[
        ToolParameter(name="name", type="string", description="Page name to overwrite"),
        ToolParameter(name="component_definition", type="object", description="Full component map (must include the rootComponent key)"),
        ToolParameter(name="root_component", type="string", description="Root component key"),
        ToolParameter(name="event_functions", type="object", required=False, description="Full eventFunctions map (omit = keep current)"),
        ToolParameter(name="properties", type="object", required=False, description="Page-level properties to set (omit = keep current)"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
        ToolParameter(name="message", type="string", required=False, description="Commit message"),
    ],
    execute=_execute_replace_page_definition,
)


# ── delete_page ──────────────────────────────────────────────────────────


async def _execute_delete_page(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    page, err = await p_ops.fetch_page_by_name(client, name, ac, headers)
    if err:
        return ToolResult(success=False, error=err)
    pid = (page or {}).get("id")
    if not pid:
        return ToolResult(success=False, error=f"Page '{name}' has no id")
    r = await client.delete(f"{p_ops.API_PREFIX}/{pid}", headers=headers)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Deleted page '{name}' (id={pid}).")


delete_page_tool = ToolDefinition(
    name="delete_page",
    description="Delete a page by name. DESTRUCTIVE — confirm before calling.",
    parameters=[
        ToolParameter(name="name", type="string", description="Page name to delete"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
    ],
    execute=_execute_delete_page,
)


# ── get_page_summary ─────────────────────────────────────────────────────


async def _execute_get_page_summary(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    page, err = await p_ops.fetch_page_by_name(client, name, ac, headers)
    if err:
        return ToolResult(success=False, error=err)
    assert page is not None
    return ToolResult(success=True, summary=json.dumps(p_ops.build_page_summary(page), indent=2, default=str))


get_page_summary_tool = ToolDefinition(
    name="get_page_summary",
    description="High-level page overview: counts, type histogram, root children with subtree sizes, top events. Safe on huge pages — use FIRST before deciding how to drill in.",
    parameters=[
        ToolParameter(name="name", type="string", description="Page name"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
    ],
    execute=_execute_get_page_summary,
)


# ── get_component_subtree ────────────────────────────────────────────────


async def _execute_get_component_subtree(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    root_key = (params.get("root_component_key") or "").strip()
    if not page_name or not root_key:
        return ToolResult(success=False, error="`page_name` and `root_component_key` are required")
    try:
        max_depth = max(1, min(int(params.get("max_depth") or 3), 10))
        max_components = max(1, min(int(params.get("max_components") or 50), 500))
    except (TypeError, ValueError):
        max_depth, max_components = 3, 50
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    page, err = await p_ops.fetch_page_by_name(client, page_name, ac, headers)
    if err:
        return ToolResult(success=False, error=err)
    assert page is not None
    body = p_ops.build_subtree(page, root_key, max_depth=max_depth, max_components=max_components)
    return ToolResult(success=True, summary=f"Subtree from '{root_key}' on page '{page_name}':\n\n{body}")


get_component_subtree_tool = ToolDefinition(
    name="get_component_subtree",
    description="Render a tree from a specific component, bounded by max_depth + max_components. Right tool for drilling into a section of a large page.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name"),
        ToolParameter(name="root_component_key", type="string", description="Component key to start from"),
        ToolParameter(name="max_depth", type="integer", required=False, default=3, description="Max nesting levels (1-10)"),
        ToolParameter(name="max_components", type="integer", required=False, default=50, description="Cap on emitted components (1-500)"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
    ],
    execute=_execute_get_component_subtree,
)


# ── search_page_components ───────────────────────────────────────────────


async def _execute_search_page_components(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    if not page_name:
        return ToolResult(success=False, error="`page_name` is required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    page, err = await p_ops.fetch_page_by_name(client, page_name, ac, headers)
    if err:
        return ToolResult(success=False, error=err)
    assert page is not None
    results = p_ops.search_components(
        page,
        component_type=params.get("component_type"),
        name_contains=params.get("name_contains"),
        text_contains=params.get("text_contains"),
        has_binding=bool(params.get("has_binding")),
        has_event_handler=bool(params.get("has_event_handler")),
    )
    if not results:
        return ToolResult(success=True, summary=f"No components matched the filters on page '{page_name}'.")
    return ToolResult(success=True, summary=f"{len(results)} matches on page '{page_name}':\n{json.dumps(results, indent=2, default=str)}")


search_page_components_tool = ToolDefinition(
    name="search_page_components",
    description="Find components on a page by type/name/text/binding/event-handler filters. Returns (key, type, name, depth) rows.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name"),
        ToolParameter(name="component_type", type="string", required=False, description="Filter by component type"),
        ToolParameter(name="name_contains", type="string", required=False, description="Case-insensitive substring on component name"),
        ToolParameter(name="text_contains", type="string", required=False, description="Substring in component properties JSON"),
        ToolParameter(name="has_binding", type="boolean", required=False, default=False, description="Only with bindingPath*"),
        ToolParameter(name="has_event_handler", type="boolean", required=False, default=False, description="Only with onClick/onChange/etc."),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
    ],
    execute=_execute_search_page_components,
)


# ── search_pages (cross-page substring scan) ─────────────────────────────


def _scan_doc_for_text(doc: dict[str, Any], needle: str, case_sensitive: bool) -> tuple[int, list[str]]:
    """Walk a doc looking for needle; return (count, up-to-3-snippets)."""
    n = needle if case_sensitive else needle.lower()
    count = 0
    snippets: list[str] = []

    def _visit(v: Any) -> None:
        nonlocal count
        if count >= 100:  # cap per-doc work
            return
        if isinstance(v, str):
            target = v if case_sensitive else v.lower()
            if n in target:
                count += 1
                if len(snippets) < 3:
                    idx = target.index(n)
                    start = max(0, idx - 30)
                    end = min(len(v), idx + len(needle) + 30)
                    snippets.append(v[start:end])
        elif isinstance(v, dict):
            for vv in v.values():
                _visit(vv)
        elif isinstance(v, list):
            for vv in v:
                _visit(vv)

    _visit(doc)
    return count, snippets


async def _execute_search_pages(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    text_contains = (params.get("text_contains") or "").strip()
    if not text_contains:
        return ToolResult(success=False, error="`text_contains` must not be empty")
    scope = (params.get("scope") or "all").strip()
    if scope not in ("all", "events_only", "components_only"):
        return ToolResult(success=False, error="`scope` must be all|events_only|components_only")
    case_sensitive = bool(params.get("case_sensitive"))
    try:
        size = max(1, min(int(params.get("size") or 200), 1000))
    except (TypeError, ValueError):
        size = 200
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    listing = await client.get(p_ops.API_PREFIX, headers=headers, params={"page": 0, "size": size, "appCode": ac})
    if not listing.success:
        return ToolResult(success=False, error=listing.error)
    stubs = (listing.data or {}).get("content", []) if isinstance(listing.data, dict) else []
    if not stubs:
        return ToolResult(success=True, summary=f"No pages in app '{ac}'.")
    details = await asyncio.gather(*(
        client.get(f"{p_ops.API_PREFIX}/{p['id']}", headers=headers) for p in stubs
    ))
    matches: list[dict[str, Any]] = []
    fetch_errors: list[dict[str, Any]] = []
    for stub, dr in zip(stubs, details):
        if not dr.success or not isinstance(dr.data, dict):
            fetch_errors.append({"name": stub.get("name"), "error": dr.error or "non-dict"})
            continue
        page = dr.data
        if scope == "events_only":
            scan_doc = {"eventFunctions": page.get("eventFunctions") or {}}
        elif scope == "components_only":
            scan_doc = {"componentDefinition": page.get("componentDefinition") or {}}
        else:
            scan_doc = page
        count, snippets = _scan_doc_for_text(scan_doc, text_contains, case_sensitive)
        if count > 0:
            matches.append({"pageName": page.get("name") or "<unnamed>", "matchCount": count, "snippets": snippets})
    matches.sort(key=lambda m: -m["matchCount"])
    header = f"{len(matches)} pages matched '{text_contains}' (scope={scope}, scanned={len(stubs) - len(fetch_errors)}/{len(stubs)})"
    body = json.dumps(matches, indent=2, default=str) if matches else "(no matches)"
    if fetch_errors:
        sample = ", ".join(f"{e['name']}({e['error']})" for e in fetch_errors[:5])
        more = f" +{len(fetch_errors) - 5} more" if len(fetch_errors) > 5 else ""
        body += f"\n\nSkipped {len(fetch_errors)} pages: {sample}{more}"
    return ToolResult(success=True, summary=f"{header}\n{body}")


search_pages_tool = ToolDefinition(
    name="search_pages",
    description="Find pages whose JSON contains text. Walks componentDefinition AND eventFunctions (catches REST URLs in step parameterMaps). Returns {pageName, matchCount, snippets[3]}.",
    parameters=[
        ToolParameter(name="text_contains", type="string", description="Substring to find (case-insensitive by default)"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
        ToolParameter(name="scope", type="string", required=False, default="all", description="all | events_only | components_only"),
        ToolParameter(name="case_sensitive", type="boolean", required=False, default=False, description="Exact-case match"),
        ToolParameter(name="size", type="integer", required=False, default=200, description="Max pages to scan (capped at 1000)"),
    ],
    execute=_execute_search_pages,
)


# ── get_component ────────────────────────────────────────────────────────


async def _execute_get_component(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    component_key = (params.get("component_key") or "").strip()
    if not page_name or not component_key:
        return ToolResult(success=False, error="`page_name` and `component_key` are required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    page, err = await p_ops.fetch_page_by_name(client, page_name, ac, headers)
    if err:
        return ToolResult(success=False, error=err)
    assert page is not None
    comp = p_ops.summarize_component(page, component_key)
    if comp is None:
        return ToolResult(success=False, error=f"Component '{component_key}' not found on page '{page_name}'.")
    return ToolResult(success=True, summary=f"Component on page '{page_name}':\n{json.dumps(comp, indent=2, default=str)}")


get_component_tool = ToolDefinition(
    name="get_component",
    description="Read one component's detail: type, properties, children keys, bindings, styleProperty keys.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name"),
        ToolParameter(name="component_key", type="string", description="Component key (UUID-ish)"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
    ],
    execute=_execute_get_component,
)


# ── get_component_styles ─────────────────────────────────────────────────


async def _execute_get_component_styles(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    component_key = (params.get("component_key") or "").strip()
    if not page_name or not component_key:
        return ToolResult(success=False, error="`page_name` and `component_key` are required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    page, err = await p_ops.fetch_page_by_name(client, page_name, ac, headers)
    if err:
        return ToolResult(success=False, error=err)
    assert page is not None
    comp = (page.get("componentDefinition") or {}).get(component_key)
    if not isinstance(comp, dict):
        return ToolResult(success=False, error=f"Component '{component_key}' not found.")
    style_props = comp.get("styleProperties") or {}
    if not style_props:
        return ToolResult(success=True, summary=f"Component '{component_key}' has no styleProperties.")

    breakpoint_filter = params.get("breakpoint")
    sub_component_filter = params.get("sub_component")
    pseudo_state_filter = params.get("pseudo_state")
    value_needle = (params.get("value_contains") or "").lower() if params.get("value_contains") else None

    report: list[dict[str, Any]] = []
    for rule_key, rule in style_props.items():
        if not isinstance(rule, dict):
            continue
        rule_pseudo = rule.get("pseudoState") or ""
        if pseudo_state_filter is not None and rule_pseudo != pseudo_state_filter:
            continue
        resolutions = rule.get("resolutions") or {}
        if not isinstance(resolutions, dict):
            continue
        leaves_by_bp: dict[str, list[dict[str, Any]]] = {}
        for bp, leaves in resolutions.items():
            if breakpoint_filter is not None and bp != breakpoint_filter:
                continue
            if not isinstance(leaves, dict):
                continue
            bucket: list[dict[str, Any]] = []
            for leaf_key, leaf_val in leaves.items():
                parsed = c.parse_css_prop_key(leaf_key)
                if sub_component_filter is not None and parsed["sub_component"] != sub_component_filter:
                    continue
                raw_val = leaf_val.get("value") if isinstance(leaf_val, dict) else leaf_val
                value_str = json.dumps(raw_val, default=str)
                if value_needle and value_needle not in value_str.lower():
                    continue
                bucket.append({
                    "leaf": leaf_key,
                    "cssProp": parsed["css_prop"],
                    "subComponent": parsed["sub_component"] or None,
                    "value": raw_val,
                })
            if bucket:
                leaves_by_bp[bp] = bucket
        if leaves_by_bp:
            report.append({
                "ruleKey": rule_key,
                "pseudoState": rule_pseudo or None,
                "condition": rule.get("condition") or None,
                "leavesByBreakpoint": leaves_by_bp,
            })

    if not report:
        return ToolResult(success=True, summary=f"No matching style leaves on '{component_key}' under current filters. Total rules: {len(style_props)}.")
    return ToolResult(success=True, summary=f"styleProperties for '{component_key}' on page '{page_name}':\n{json.dumps(report, indent=2, default=str)}")


get_component_styles_tool = ToolDefinition(
    name="get_component_styles",
    description="Read full styleProperties leaves for one component, parsed into (sub, cssProp, pseudo) and grouped by rule/breakpoint. Optional filters: breakpoint, sub_component, pseudo_state, value_contains.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name"),
        ToolParameter(name="component_key", type="string", description="Component key"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
        ToolParameter(name="breakpoint", type="string", required=False, description="Filter: only this breakpoint"),
        ToolParameter(name="sub_component", type="string", required=False, description="Filter: only leaves under this sub"),
        ToolParameter(name="pseudo_state", type="string", required=False, description="Filter: only rules with this pseudoState"),
        ToolParameter(name="value_contains", type="string", required=False, description="Filter: substring in leaf value"),
    ],
    execute=_execute_get_component_styles,
)


# ── add_component ────────────────────────────────────────────────────────


async def _execute_add_component(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    parent_key = (params.get("parent_key") or "").strip()
    component_type = (params.get("component_type") or "").strip()
    if not page_name or not parent_key or not component_type:
        return ToolResult(success=False, error="`page_name`, `parent_key`, `component_type` are required")

    properties = params.get("properties")
    verr = _validate_properties(component_type, properties)
    if verr:
        return ToolResult(success=False, error=verr)

    wrapped_properties = c.wrap_props_catalog_aware(component_type, properties, None) if properties else None
    key = (params.get("component_key") or "").strip() or str(uuid.uuid4())

    def mutate(page: dict[str, Any]) -> str | None:
        return p_ops.add_component(
            page,
            parent_key=parent_key,
            component_key=key,
            component_type=component_type,
            name=params.get("name") or component_type.lower(),
            properties=wrapped_properties,
            style_properties=params.get("style_properties"),
            binding_paths=params.get("binding_paths"),
            display_order=int(params.get("display_order") or 0),
        )

    ok, err = await _load_save(page_name, context, params, mutate, params.get("message") or "Added component via CFA")
    if not ok:
        return ToolResult(success=False, error=err)
    return ToolResult(success=True, summary=f"Added {component_type} '{key}' under '{parent_key}' on page '{page_name}'.")


add_component_tool = ToolDefinition(
    name="add_component",
    description="""Add a new component under `parent_key` on a page. Returns the new component's key. Use this to populate a freshly-created page or to insert a new component into an existing layout.

Common shapes:

Add a Button under the root Grid:
```
add_component(
    page_name="contact",
    parent_key="root",
    component_type="Button",
    component_key="submitBtn",
    properties={"label": "Submit", "onClick": "handleSubmit"}
)
```

Add a TextBox with two-way data binding (the input writes into Page.user.email):
```
add_component(
    page_name="contact",
    parent_key="formGrid",
    component_type="TextBox",
    component_key="emailInput",
    properties={"label": "Email", "placeholder": "you@example.com"},
    binding_paths={"bindingPath": {"value": "Page.user.email"}}
)
```

Key rules:
- `parent_key="root"` is the top of the page tree. `root` always exists after `create_page`. Other valid parents are any Grid/Container/etc. you've already added.
- `component_type` is the catalog name: `Button`, `TextBox`, `Grid`, `Dropdown`, `Table`, `Image`, etc. Use the `components` group to look up unfamiliar types via `list_component_types` or `get_component_schema`.
- `component_key` is optional — provide a meaningful slug (`submitBtn`, `emailInput`) for readability. If omitted, a UUID is generated.
- `properties` values are auto-wrapped: pass plain values like `{"label": "Submit"}` and the tool wraps them into `{"label": {"value": "Submit"}}`. For expression bindings, pass the dict shape directly: `{"label": {"location": {"type": "EXPRESSION", "value": "Page.formTitle"}}}`.
- `binding_paths` is for `bindingPath` / `bindingPath2`…`bindingPath6` ONLY — see the component-types reference for which components need binding (TextBox, Dropdown, ArrayRepeater, Table, etc.).
- `style_properties` follows the same nested shape as `patch_component_styles` — see the styling walkthrough for the exact structure.

For multiple sibling components, call `add_component` once per component. There's no bulk-add — but each call is cheap (single PATCH).""",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page to modify"),
        ToolParameter(name="parent_key", type="string", description="Parent component key (use 'root' for page root)"),
        ToolParameter(name="component_type", type="string", description="Catalog type, e.g. 'Button'"),
        ToolParameter(name="properties", type="object", required=False, description="Raw prop values, e.g. {label:'Save'}"),
        ToolParameter(name="style_properties", type="object", required=False, description="Nested {resolutions:{ALL:{...}}}"),
        ToolParameter(name="binding_paths", type="object", required=False, description="{bindingPath: {type:VALUE, value:'Page.x'}}"),
        ToolParameter(name="component_key", type="string", required=False, description="Optional explicit key (auto-UUID)"),
        ToolParameter(name="name", type="string", required=False, description="Display name (default: type lowercased)"),
        ToolParameter(name="display_order", type="integer", required=False, default=0, description="Sort order within siblings"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
        ToolParameter(name="message", type="string", required=False, description="Commit message"),
    ],
    execute=_execute_add_component,
)


# ── update_component_props ───────────────────────────────────────────────


async def _execute_update_component_props(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    component_key = (params.get("component_key") or "").strip()
    properties = params.get("properties") or {}
    if not page_name or not component_key or not isinstance(properties, dict):
        return ToolResult(success=False, error="`page_name`, `component_key`, `properties` (dict) are required")

    def mutate(page: dict[str, Any]) -> str | None:
        comp = (page.get("componentDefinition") or {}).get(component_key)
        if comp is None:
            return f"Component '{component_key}' not found"
        verr = _validate_properties(comp.get("type") or "", properties)
        if verr:
            return verr
        wrapped = c.wrap_props_catalog_aware(comp.get("type") or "", properties, comp.get("properties") or {})
        return p_ops.update_component(page, component_key=component_key, properties=wrapped)

    ok, err = await _load_save(page_name, context, params, mutate, params.get("message") or "Updated component props via CFA")
    if not ok:
        return ToolResult(success=False, error=err)
    return ToolResult(success=True, summary=f"Updated properties on '{component_key}' in page '{page_name}': {list(properties.keys())}")


update_component_props_tool = ToolDefinition(
    name="update_component_props",
    description="Merge property values into an existing component. Catalog-aware multi-valued wrapping for animation/validation/etc.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name"),
        ToolParameter(name="component_key", type="string", description="Component key"),
        ToolParameter(name="properties", type="object", description="Raw prop values to merge"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
        ToolParameter(name="message", type="string", required=False, description="Commit message"),
    ],
    execute=_execute_update_component_props,
)


# ── set_styles (merge|replace) ───────────────────────────────────────────


async def _execute_set_styles(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    component_key = (params.get("component_key") or "").strip()
    style_properties = params.get("style_properties") or {}
    mode = (params.get("mode") or "merge").strip()
    if not page_name or not component_key or not isinstance(style_properties, dict):
        return ToolResult(success=False, error="`page_name`, `component_key`, `style_properties` (dict) are required")
    if mode not in ("merge", "replace"):
        return ToolResult(success=False, error="`mode` must be 'merge' or 'replace'")

    def mutate(page: dict[str, Any]) -> str | None:
        if mode == "merge":
            return p_ops.update_component(page, component_key=component_key, style_properties=style_properties)
        # Replace
        comp_def = page.setdefault("componentDefinition", {})
        if component_key not in comp_def:
            return f"Component '{component_key}' not found"
        target = comp_def[component_key].setdefault("styleProperties", {})
        for rule_key, rule_value in style_properties.items():
            if not isinstance(rule_value, dict):
                target[rule_key] = rule_value
                continue
            existing_rule = target.setdefault(rule_key, {})
            input_res = rule_value.get("resolutions") or {}
            existing_res = existing_rule.setdefault("resolutions", {})
            for bp, leaves in input_res.items():
                existing_res[bp] = dict(leaves) if isinstance(leaves, dict) else leaves
            for k, v in rule_value.items():
                if k != "resolutions":
                    existing_rule[k] = v
        return None

    ok, err = await _load_save(page_name, context, params, mutate, params.get("message") or "Updated styles via CFA")
    if not ok:
        return ToolResult(success=False, error=err)
    return ToolResult(success=True, summary=f"Updated styles on '{component_key}' in page '{page_name}' (mode={mode}).")


set_styles_tool = ToolDefinition(
    name="set_styles",
    description="Write styleProperties on a component. mode='merge' (default) deep-merges (omitted leaves SURVIVE). mode='replace' makes input authoritative at (rule, breakpoint) — omitted leaves DROPPED. For leaf-level removal use remove_component_styles; for whole-rule wipe use delete_style_rule.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name"),
        ToolParameter(name="component_key", type="string", description="Component key"),
        ToolParameter(name="style_properties", type="object", description="Nested {ruleKey:{resolutions:{bp:{cssProp:{value:...}}}}}"),
        ToolParameter(name="mode", type="string", required=False, default="merge", description="merge | replace"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
        ToolParameter(name="message", type="string", required=False, description="Commit message"),
    ],
    execute=_execute_set_styles,
)


# ── delete_style_rule ────────────────────────────────────────────────────


async def _execute_delete_style_rule(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    component_key = (params.get("component_key") or "").strip()
    rule_key = (params.get("rule_key") or "").strip()
    if not page_name or not component_key or not rule_key:
        return ToolResult(success=False, error="`page_name`, `component_key`, `rule_key` are required")

    deleted = {"hit": False}

    def mutate(page: dict[str, Any]) -> str | None:
        comp_def = page.setdefault("componentDefinition", {})
        if component_key not in comp_def:
            return f"Component '{component_key}' not found"
        style_props = comp_def[component_key].get("styleProperties") or {}
        if rule_key in style_props:
            style_props.pop(rule_key, None)
            deleted["hit"] = True
        return None

    ok, err = await _load_save(page_name, context, params, mutate, params.get("message") or "Deleted style rule via CFA")
    if not ok:
        return ToolResult(success=False, error=err)
    if not deleted["hit"]:
        return ToolResult(success=True, summary=f"No-op: rule '{rule_key}' not found on '{component_key}'.")
    return ToolResult(success=True, summary=f"Deleted style rule '{rule_key}' from '{component_key}' in page '{page_name}'.")


delete_style_rule_tool = ToolDefinition(
    name="delete_style_rule",
    description="Delete one whole style rule from a component's styleProperties (other rules untouched). For leaf-level removal use remove_component_styles.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name"),
        ToolParameter(name="component_key", type="string", description="Component key"),
        ToolParameter(name="rule_key", type="string", description="Rule key (UUID or 'comp')"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
        ToolParameter(name="message", type="string", required=False, description="Commit message"),
    ],
    execute=_execute_delete_style_rule,
)


# ── set_bindings ─────────────────────────────────────────────────────────


async def _execute_set_bindings(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    component_key = (params.get("component_key") or "").strip()
    binding_paths = params.get("binding_paths") or {}
    if not page_name or not component_key or not isinstance(binding_paths, dict):
        return ToolResult(success=False, error="`page_name`, `component_key`, `binding_paths` (dict) are required")

    def mutate(page: dict[str, Any]) -> str | None:
        return p_ops.update_component(page, component_key=component_key, binding_paths=binding_paths)

    ok, err = await _load_save(page_name, context, params, mutate, params.get("message") or "Updated bindings via CFA")
    if not ok:
        return ToolResult(success=False, error=err)
    return ToolResult(success=True, summary=f"Set bindings {list(binding_paths.keys())} on '{component_key}' in page '{page_name}'.")


set_bindings_tool = ToolDefinition(
    name="set_bindings",
    description="Set one or more bindingPath* keys on a component (bindingPath, bindingPath2, ...).",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name"),
        ToolParameter(name="component_key", type="string", description="Component key"),
        ToolParameter(name="binding_paths", type="object", description="Map of {bindingPath: {type, value/expression}}"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
        ToolParameter(name="message", type="string", required=False, description="Commit message"),
    ],
    execute=_execute_set_bindings,
)


# ── move_component ───────────────────────────────────────────────────────


async def _execute_move_component(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    component_key = (params.get("component_key") or "").strip()
    new_parent_key = (params.get("new_parent_key") or "").strip()
    if not page_name or not component_key or not new_parent_key:
        return ToolResult(success=False, error="`page_name`, `component_key`, `new_parent_key` are required")
    display_order = params.get("display_order")

    def mutate(page: dict[str, Any]) -> str | None:
        return p_ops.move_component(
            page, component_key=component_key, new_parent_key=new_parent_key,
            display_order=int(display_order) if display_order is not None else None,
        )

    ok, err = await _load_save(page_name, context, params, mutate, params.get("message") or "Moved component via CFA")
    if not ok:
        return ToolResult(success=False, error=err)
    return ToolResult(success=True, summary=f"Moved '{component_key}' under '{new_parent_key}' in page '{page_name}'.")


move_component_tool = ToolDefinition(
    name="move_component",
    description="Re-parent a component to a different container. Cannot move the root.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name"),
        ToolParameter(name="component_key", type="string", description="Component key to move"),
        ToolParameter(name="new_parent_key", type="string", description="Destination parent key"),
        ToolParameter(name="display_order", type="integer", required=False, description="Optional new displayOrder"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
        ToolParameter(name="message", type="string", required=False, description="Commit message"),
    ],
    execute=_execute_move_component,
)


# ── remove_component ─────────────────────────────────────────────────────


async def _execute_remove_component(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    component_key = (params.get("component_key") or "").strip()
    if not page_name or not component_key:
        return ToolResult(success=False, error="`page_name`, `component_key` are required")
    recursive = bool(params.get("recursive", True))

    def mutate(page: dict[str, Any]) -> str | None:
        return p_ops.remove_component(page, component_key=component_key, recursive=recursive)

    ok, err = await _load_save(page_name, context, params, mutate, params.get("message") or "Removed component via CFA")
    if not ok:
        return ToolResult(success=False, error=err)
    return ToolResult(success=True, summary=f"Removed '{component_key}' from page '{page_name}'.")


remove_component_tool = ToolDefinition(
    name="remove_component",
    description="Remove a component (and its descendants by default). DESTRUCTIVE.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name"),
        ToolParameter(name="component_key", type="string", description="Component key to remove"),
        ToolParameter(name="recursive", type="boolean", required=False, default=True, description="Also remove descendants"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
        ToolParameter(name="message", type="string", required=False, description="Commit message"),
    ],
    execute=_execute_remove_component,
)


# ── rename_component ─────────────────────────────────────────────────────


async def _execute_rename_component(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    old_key = (params.get("old_key") or "").strip()
    new_key = (params.get("new_key") or "").strip()
    if not page_name or not old_key or not new_key:
        return ToolResult(success=False, error="`page_name`, `old_key`, `new_key` are required")
    if old_key == new_key:
        return ToolResult(success=False, error="old_key and new_key are identical")
    name_err = c.validate_simple_name(new_key)
    if name_err:
        return ToolResult(success=False, error=name_err)
    rename_display_name = bool(params.get("rename_display_name", True))

    stats = {"children_updates": 0}

    def mutate(page: dict[str, Any]) -> str | None:
        comp_def = page.setdefault("componentDefinition", {})
        if old_key not in comp_def:
            return f"Component '{old_key}' not found"
        if new_key in comp_def:
            return f"Cannot rename: key '{new_key}' already exists"
        comp = comp_def.pop(old_key)
        if isinstance(comp, dict):
            comp["key"] = new_key
            if rename_display_name and comp.get("name") == old_key:
                comp["name"] = new_key
        comp_def[new_key] = comp
        for other in comp_def.values():
            if not isinstance(other, dict):
                continue
            children = other.get("children")
            if isinstance(children, dict) and old_key in children:
                children[new_key] = children.pop(old_key)
                stats["children_updates"] += 1
        if page.get("rootComponent") == old_key:
            page["rootComponent"] = new_key
        cv = page.get("componentVersions")
        if isinstance(cv, dict) and old_key in cv:
            cv[new_key] = cv.pop(old_key)
        return None

    ok, err = await _load_save(page_name, context, params, mutate, params.get("message") or "Renamed component via CFA")
    if not ok:
        return ToolResult(success=False, error=err)
    return ToolResult(success=True, summary=f"Renamed '{old_key}' → '{new_key}' on page '{page_name}' (updated {stats['children_updates']} parent reference(s)).")


rename_component_tool = ToolDefinition(
    name="rename_component",
    description="Rename a component's key in place, fixing every internal reference (children, rootComponent, componentVersions). Maintains the platform invariant componentDefinition[K].key === K.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name"),
        ToolParameter(name="old_key", type="string", description="Current component key"),
        ToolParameter(name="new_key", type="string", description="New key (must not collide)"),
        ToolParameter(name="rename_display_name", type="boolean", required=False, default=True, description="Also update .name field if it matched old_key"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
        ToolParameter(name="message", type="string", required=False, description="Commit message"),
    ],
    execute=_execute_rename_component,
)


# ── bulk_patch_component_props ───────────────────────────────────────────


async def _execute_bulk_patch_component_props(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    filt = params.get("filter") or {}
    properties = params.get("properties") or {}
    dry_run = bool(params.get("dry_run", False))
    if not page_name or not isinstance(filt, dict) or not isinstance(properties, dict):
        return ToolResult(success=False, error="`page_name`, `filter` (dict), `properties` (dict) are required")

    try:
        key_re = re.compile(filt["key_pattern"]) if filt.get("key_pattern") else None
    except re.error as e:
        return ToolResult(success=False, error=f"Invalid key_pattern regex: {e}")

    wanted_type = filt.get("type")
    wanted_keys = set(filt.get("keys") or [])
    name_substr = (filt.get("name_contains") or "").lower()

    def _matches(key: str, comp: dict[str, Any]) -> bool:
        if wanted_type and comp.get("type") != wanted_type:
            return False
        if wanted_keys and key not in wanted_keys:
            return False
        if key_re and not key_re.search(key):
            return False
        if name_substr and name_substr not in (comp.get("name") or "").lower():
            return False
        return True

    matched_keys: list[str] = []

    def mutate(page: dict[str, Any]) -> str | None:
        comp_def = page.get("componentDefinition") or {}
        matches = [(k, v) for k, v in comp_def.items() if isinstance(v, dict) and _matches(k, v)]
        if not matches:
            return "No components matched the filter."
        seen_types: set[str] = set()
        for _, comp in matches:
            ct = comp.get("type") or ""
            if ct in seen_types:
                continue
            seen_types.add(ct)
            verr = _validate_properties(ct, properties)
            if verr:
                return f"Validation failed for type {ct!r}: {verr}"
        matched_keys.extend(k for k, _ in matches)
        if dry_run:
            return None
        for _key, comp in matches:
            existing_props = dict(comp.get("properties") or {})
            wrapped = c.wrap_props_catalog_aware(comp.get("type") or "", properties, existing_props)
            existing_props.update(wrapped)
            comp["properties"] = existing_props
        return None

    if dry_run:
        # Read-only dry-run path — don't go through _load_save (would still save).
        ac, err_result = _resolve_app_code(params, context)
        if err_result:
            return err_result
        client, headers = _client_and_headers(context)
        page, err = await p_ops.fetch_page_by_name(client, page_name, ac, headers)
        if err:
            return ToolResult(success=False, error=err)
        assert page is not None
        merr = mutate(page)
        if merr:
            return ToolResult(success=False, error=merr)
        preview = ", ".join(matched_keys[:20])
        more = f" (+{len(matched_keys) - 20} more)" if len(matched_keys) > 20 else ""
        return ToolResult(success=True, summary=f"[dry-run] Would patch {len(matched_keys)} component(s) on '{page_name}': {preview}{more}")

    ok, err = await _load_save(page_name, context, params, mutate, params.get("message") or "Bulk patched via CFA")
    if not ok:
        return ToolResult(success=False, error=err)
    preview = ", ".join(matched_keys[:20])
    more = f" (+{len(matched_keys) - 20} more)" if len(matched_keys) > 20 else ""
    return ToolResult(success=True, summary=f"Patched {len(matched_keys)} component(s) on '{page_name}': {preview}{more}")


bulk_patch_component_props_tool = ToolDefinition(
    name="bulk_patch_component_props",
    description="""Apply ONE properties patch to EVERY component on a page that matches a filter. One atomic save, one network round-trip. The right tool when changing the same prop on N components.

Filter shapes (combine as needed — all matchers AND together):
- `{"type": "Button"}` — every Button on the page
- `{"keys": ["btn1", "btn2", "btn3"]}` — explicit list of component keys
- `{"key_pattern": "^primary"}` — regex over keys (anchor with `^` / `$` as needed)
- `{"name_contains": "submit"}` — substring match on the component's display name
- Combined: `{"type": "Button", "name_contains": "primary"}` — every Button whose name contains "primary"

Example — set every Button's backgroundColor to the theme primary:
```
bulk_patch_component_props(
    page_name="home",
    filter={"type": "Button"},
    properties={"backgroundColor": {"location": {"type": "EXPRESSION", "value": "Theme.primaryColor"}}}
)
```

IMPORTANT — use `dry_run=true` FIRST when you're unsure which components will match. It returns the matched keys without saving so you can sanity-check. Then re-call without `dry_run` to apply.

Use this INSTEAD OF N `patch_component_props` calls:
- 10 Buttons via 10 `patch_component_props` calls = 10 network round-trips = 10× the latency.
- 10 Buttons via 1 `bulk_patch_component_props` call = 1 round-trip = same outcome, 10× faster.

NOT the right tool when each component needs a DIFFERENT properties patch (e.g. "make button1 red and button2 blue") — for that, you need N `patch_component_props` calls, one per component.""",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name"),
        ToolParameter(name="filter", type="object", description="Any of: type, keys[], key_pattern, name_contains"),
        ToolParameter(name="properties", type="object", description="Property patch to apply to each match"),
        ToolParameter(name="dry_run", type="boolean", required=False, default=False, description="Preview matched keys, no save"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
        ToolParameter(name="message", type="string", required=False, description="Commit message"),
    ],
    execute=_execute_bulk_patch_component_props,
)


# ── composition_v2: per-component surgical PATCH ─────────────────────────


PAGES_PATCH_PREFIX = p_ops.API_PREFIX  # /api/ui/pages


async def _patch_component_on_server(
    page_name: str, component_key: str, updated_comp: dict[str, Any],
    context: dict[str, Any], message: str,
) -> tuple[bool, str]:
    """PATCH /api/ui/pages/{id}/components/{key} on the platform."""
    ac, err_result = _resolve_app_code({}, context)
    if err_result:
        return False, err_result.error
    client, headers = _client_and_headers(context)
    page, err = await p_ops.fetch_page_by_name(client, page_name, ac, headers)
    if err:
        return False, err
    assert page is not None
    page_id = page.get("id")
    if not page_id:
        return False, "Fetched page has no id"
    expected = c.component_version_for(page, component_key)
    # Platform body shape: ComponentPatchRequest expects `componentData`, not `component`.
    # Sending the wrong key NPEs the platform at PageService.patchComponent (updated is null).
    body = {
        "componentData": updated_comp,
        "expectedComponentVersion": expected,
        "message": message,
    }
    r = await client.patch(f"{PAGES_PATCH_PREFIX}/{page_id}/components/{component_key}", headers=headers, json=body)
    if not r.success:
        return False, r.error
    return True, ""


async def _execute_patch_component_props(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    component_key = (params.get("component_key") or "").strip()
    properties = params.get("properties") or {}
    if not page_name or not component_key or not isinstance(properties, dict):
        return ToolResult(success=False, error="`page_name`, `component_key`, `properties` (dict) are required")

    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    page, err = await p_ops.fetch_page_by_name(client, page_name, ac, headers)
    if err:
        return ToolResult(success=False, error=err)
    assert page is not None
    comp = (page.get("componentDefinition") or {}).get(component_key)
    if not isinstance(comp, dict):
        return ToolResult(success=False, error=f"Component '{component_key}' not found")
    verr = _validate_properties(comp.get("type") or "", properties)
    if verr:
        return ToolResult(success=False, error=verr)
    existing = dict(comp)
    existing_props = dict(existing.get("properties") or {})
    wrapped = c.wrap_props_catalog_aware(comp.get("type") or "", properties, existing_props)
    existing_props.update(wrapped)
    existing["properties"] = existing_props
    ok, err = await _patch_component_on_server(page_name, component_key, existing, context, params.get("message") or "Patched props via CFA")
    if not ok:
        return ToolResult(success=False, error=err)
    return ToolResult(success=True, summary=f"Patched props on '{component_key}': {list(properties.keys())}")


patch_component_props_tool = ToolDefinition(
    name="patch_component_props",
    description="""Surgical PATCH of ONE component's properties. The component's other props stay untouched — only the keys in `properties` are merged.

Use when changing 1-2 props on 1 component. For multiple components with the SAME patch, use `bulk_patch_component_props` instead.

Common shapes:

Wire a button's onClick to an event function:
```
patch_component_props(
    page_name="login",
    component_key="signInBtn",
    properties={"onClick": {"value": "handleSignIn"}}
)
```
The `value` is the event function NAME (camelCase slug), not the function's body. The function must already exist via `create_page_event_function` / `save_page_event_function_from_text`.

Set a label or placeholder (literal string):
```
patch_component_props(
    page_name="contact",
    component_key="emailInput",
    properties={"label": {"value": "Email address"}, "placeholder": {"value": "you@example.com"}}
)
```

Reference theme color (expression):
```
patch_component_props(
    page_name="contact",
    component_key="title",
    properties={"color": {"location": {"type": "EXPRESSION", "value": "Theme.primaryColor"}}}
)
```

Bind a property dynamically (e.g. show/hide based on store):
```
patch_component_props(
    page_name="contact",
    component_key="successPanel",
    properties={"visibility": {"location": {"type": "EXPRESSION", "value": "Page.formSubmitted"}}}
)
```

Hard rules:
- EVERY value MUST be a ComponentProperty object (`{"value": ...}` or `{"location": {...}}`). Bare strings/booleans WILL be rejected.
- Static literal → `{"value": "Submit"}`. Expression → `{"location": {"type": "EXPRESSION", "value": "Theme.primaryColor"}}`. NEVER mix them at the same level except for static-with-dynamic-override (advanced).
- `visibility` is visible-when-true. To HIDE based on a condition, the expression must evaluate to false (the `not` keyword is NOT supported — invert the condition).""",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name"),
        ToolParameter(name="component_key", type="string", description="Component key (find via `get_page_summary` or `search_page_components`)"),
        ToolParameter(name="properties", type="object", description="Map of {propName: ComponentProperty} — see description for shape"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
        ToolParameter(name="message", type="string", required=False, description="Commit message"),
    ],
    execute=_execute_patch_component_props,
)


async def _execute_patch_component_bindings(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    component_key = (params.get("component_key") or "").strip()
    binding_paths = params.get("binding_paths") or {}
    if not page_name or not component_key or not isinstance(binding_paths, dict):
        return ToolResult(success=False, error="`page_name`, `component_key`, `binding_paths` (dict) are required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    page, err = await p_ops.fetch_page_by_name(client, page_name, ac, headers)
    if err:
        return ToolResult(success=False, error=err)
    assert page is not None
    comp = (page.get("componentDefinition") or {}).get(component_key)
    if not isinstance(comp, dict):
        return ToolResult(success=False, error=f"Component '{component_key}' not found")
    existing = dict(comp)
    for k, v in binding_paths.items():
        existing[k] = v
    ok, err = await _patch_component_on_server(page_name, component_key, existing, context, params.get("message") or "Patched bindings via CFA")
    if not ok:
        return ToolResult(success=False, error=err)
    return ToolResult(success=True, summary=f"Patched bindings on '{component_key}': {list(binding_paths.keys())}")


patch_component_bindings_tool = ToolDefinition(
    name="patch_component_bindings",
    description="Surgical PATCH of one component's bindingPath* keys (optimistic-locked per component).",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name"),
        ToolParameter(name="component_key", type="string", description="Component key"),
        ToolParameter(name="binding_paths", type="object", description="Map of {bindingPath: {type, value/expression}}"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
        ToolParameter(name="message", type="string", required=False, description="Commit message"),
    ],
    execute=_execute_patch_component_bindings,
)


async def _execute_patch_component_styles(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    component_key = (params.get("component_key") or "").strip()
    css_props = params.get("css_props") or {}
    breakpoint_str = (params.get("breakpoint") or "ALL").strip()
    sub_component = (params.get("sub_component") or "").strip()
    pseudo_state = (params.get("pseudo_state") or "").strip()
    if not page_name or not component_key or not isinstance(css_props, dict):
        return ToolResult(success=False, error="`page_name`, `component_key`, `css_props` (dict) are required")
    be = c.validate_breakpoint(breakpoint_str)
    if be:
        return ToolResult(success=False, error=be)

    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    page, err = await p_ops.fetch_page_by_name(client, page_name, ac, headers)
    if err:
        return ToolResult(success=False, error=err)
    assert page is not None
    comp = (page.get("componentDefinition") or {}).get(component_key)
    if not isinstance(comp, dict):
        return ToolResult(success=False, error=f"Component '{component_key}' not found")
    existing = dict(comp)
    style_props = dict(existing.get("styleProperties") or {})

    # Reuse-or-create a rule per (condition=∅, pseudoState).
    target_pseudo = pseudo_state or ""
    rule_key: str | None = None
    for rk, rv in style_props.items():
        if not isinstance(rv, dict) or rv.get("condition"):
            continue
        if (rv.get("pseudoState") or "") == target_pseudo:
            rule_key = rk
            break
    if rule_key is None:
        rule_key = uuid.uuid4().hex
        style_props[rule_key] = {"resolutions": {}}
        if pseudo_state:
            style_props[rule_key]["pseudoState"] = pseudo_state

    rule = dict(style_props[rule_key])
    resolutions = dict(rule.get("resolutions") or {})
    bp_block = dict(resolutions.get(breakpoint_str) or {})
    applied: list[str] = []
    for css_prop, css_value in css_props.items():
        leaf = c.make_css_prop_key(css_prop, sub_component, "")
        bp_block[leaf] = {"value": css_value}
        applied.append(leaf)
    resolutions[breakpoint_str] = bp_block
    rule["resolutions"] = resolutions
    style_props[rule_key] = rule
    existing["styleProperties"] = style_props

    ok, err = await _patch_component_on_server(page_name, component_key, existing, context, params.get("message") or "Patched styles via CFA")
    if not ok:
        return ToolResult(success=False, error=err)
    scope = [breakpoint_str]
    if sub_component:
        scope.append(f"sub={sub_component}")
    if pseudo_state:
        scope.append(f":{pseudo_state}")
    return ToolResult(success=True, summary=f"Patched styles on '{component_key}' [{', '.join(scope)}]: {applied}")


patch_component_styles_tool = ToolDefinition(
    name="patch_component_styles",
    description="""Surgical PATCH of CSS props on one component. Pass a FLAT `css_props` map — the tool builds the nested resolutions/breakpoint structure internally.

Example — theme-aware button styling:
```
patch_component_styles(
    page_name="contact",
    component_key="submitBtn",
    css_props={
        "backgroundColor": {"location": {"type": "EXPRESSION", "value": "Theme.primaryColor"}},
        "color": {"value": "#FFFFFF"},
        "paddingLeft": {"value": "24px"},
        "borderRadius": {"value": "8px"}
    }
)
```

Shape rules:
- `css_props` keys are camelCase CSS prop names: `backgroundColor`, `paddingLeft`, `fontSize`. NEVER kebab-case (`background-color`) or shorthand (`padding`).
- Each VALUE is a ComponentProperty:
  - Literal: `{"value": "16px"}`
  - Expression: `{"location": {"type": "EXPRESSION", "value": "Theme.primaryColor"}}`
  - NEVER `"16px"` or `Theme.primaryColor` as a bare string.

For scoped styling, use the dedicated params (don't bake them into css_props keys):
- `sub_component="label"` → applies to the inner `label` sub-component of the target.
- `pseudo_state="hover"` → hover-state override.
- `breakpoint="DESKTOP_SCREEN"` → desktop-only (default `ALL` covers every breakpoint).

For multi-component edits (e.g. style EVERY Button), use `bulk_patch_component_props` with a `filter` matcher instead — one round-trip, atomic.""",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name"),
        ToolParameter(name="component_key", type="string", description="Component key"),
        ToolParameter(name="css_props", type="object", description="cssProp → value map (camelCase keys)"),
        ToolParameter(name="breakpoint", type="string", required=False, default="ALL", description="ALL | DESKTOP_SCREEN | MOBILE_LANDSCAPE_SCREEN_SMALL | ..."),
        ToolParameter(name="sub_component", type="string", required=False, description="Sub-component scope (e.g. 'text', 'image')"),
        ToolParameter(name="pseudo_state", type="string", required=False, description="hover|focus|active|... (default base)"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
        ToolParameter(name="message", type="string", required=False, description="Commit message"),
    ],
    execute=_execute_patch_component_styles,
)


async def _execute_remove_component_styles(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    component_key = (params.get("component_key") or "").strip()
    css_props_list = params.get("css_props") or []
    breakpoint_str = (params.get("breakpoint") or "ALL").strip()
    sub_component = (params.get("sub_component") or "").strip()
    pseudo_state = (params.get("pseudo_state") or "").strip()
    cleanup_empty_rules = bool(params.get("cleanup_empty_rules", True))
    if not page_name or not component_key or not isinstance(css_props_list, list) or not css_props_list:
        return ToolResult(success=False, error="`page_name`, `component_key`, `css_props` (non-empty list) are required")
    be = c.validate_breakpoint(breakpoint_str)
    if be:
        return ToolResult(success=False, error=be)

    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    page, err = await p_ops.fetch_page_by_name(client, page_name, ac, headers)
    if err:
        return ToolResult(success=False, error=err)
    assert page is not None
    comp = (page.get("componentDefinition") or {}).get(component_key)
    if not isinstance(comp, dict):
        return ToolResult(success=False, error=f"Component '{component_key}' not found")
    existing = dict(comp)
    style_props = dict(existing.get("styleProperties") or {})
    target_pseudo = pseudo_state or ""
    leaves_to_drop = {c.make_css_prop_key(p, sub_component, "") for p in css_props_list}

    removed: list[str] = []
    for rk in list(style_props.keys()):
        rv = style_props[rk]
        if not isinstance(rv, dict):
            continue
        rule_pseudo = rv.get("pseudoState") or ""
        if rule_pseudo != target_pseudo:
            continue
        resolutions = dict(rv.get("resolutions") or {})
        bp_block = dict(resolutions.get(breakpoint_str) or {})
        for leaf in list(bp_block.keys()):
            if leaf in leaves_to_drop:
                bp_block.pop(leaf)
                removed.append(leaf)
        if bp_block:
            resolutions[breakpoint_str] = bp_block
        else:
            resolutions.pop(breakpoint_str, None)
        if resolutions:
            rv = {**rv, "resolutions": resolutions}
            style_props[rk] = rv
        elif cleanup_empty_rules:
            style_props.pop(rk, None)
        else:
            rv = {**rv, "resolutions": {}}
            style_props[rk] = rv
    existing["styleProperties"] = style_props

    ok, err = await _patch_component_on_server(page_name, component_key, existing, context, params.get("message") or "Removed styles via CFA")
    if not ok:
        return ToolResult(success=False, error=err)
    if not removed:
        return ToolResult(success=True, summary=f"No-op: no matching leaves on '{component_key}'.")
    return ToolResult(success=True, summary=f"Removed {len(removed)} leaf/leaves from '{component_key}' [{breakpoint_str}]: {removed}")


remove_component_styles_tool = ToolDefinition(
    name="remove_component_styles",
    description="Surgical removal of specific cssProp leaves from one component (inverse of patch_component_styles). Auto-cleans empty rules.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name"),
        ToolParameter(name="component_key", type="string", description="Component key"),
        ToolParameter(name="css_props", type="array", description="CSS prop names to remove (camelCase)", items={"type": "string"}),
        ToolParameter(name="breakpoint", type="string", required=False, default="ALL", description="Breakpoint to remove from"),
        ToolParameter(name="sub_component", type="string", required=False, description="Optional sub-component filter"),
        ToolParameter(name="pseudo_state", type="string", required=False, description="Optional pseudo-state filter"),
        ToolParameter(name="cleanup_empty_rules", type="boolean", required=False, default=True, description="Drop rules emptied by the removal"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to session"),
        ToolParameter(name="message", type="string", required=False, description="Commit message"),
    ],
    execute=_execute_remove_component_styles,
)


# ── Module export ────────────────────────────────────────────────────────


TOOLS: list[ToolDefinition] = [
    # Page CRUD + reads
    list_pages_tool,
    get_page_tool,
    create_page_tool,
    update_page_tool,
    reset_page_composition_tool,
    replace_page_definition_tool,
    delete_page_tool,
    get_page_summary_tool,
    get_component_subtree_tool,
    search_page_components_tool,
    search_pages_tool,
    get_component_tool,
    get_component_styles_tool,
    # Composition (load-modify-save)
    add_component_tool,
    update_component_props_tool,
    set_styles_tool,
    delete_style_rule_tool,
    set_bindings_tool,
    move_component_tool,
    remove_component_tool,
    rename_component_tool,
    bulk_patch_component_props_tool,
    # Composition v2 (surgical PATCH)
    patch_component_props_tool,
    patch_component_bindings_tool,
    patch_component_styles_tool,
    remove_component_styles_tool,
]
