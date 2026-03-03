"""Batch page operations — apply multiple component changes in a single fetch+save.

Instead of N×(fetch → modify → save), this fetches the page once,
applies every operation in memory, then saves once.

Supported operations:
  add    — add a new component under a parent
  update — merge properties/styles into an existing component
  remove — remove a component (and optionally all its descendants)
  move   — move a component to a different parent
"""

from __future__ import annotations

from typing import Any

from app.core.tools.base import ToolDefinition, ToolResult, ToolParameter
from app.agents.appbuilder.tools._executor import fetch_page_by_name, save_page


def _get_client_and_headers(context: dict[str, Any]) -> tuple:
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context["headers"]


# ── Operation handlers (work on comp_def in-place) ──────────────

def _op_add(comp_def: dict, op: dict) -> str | None:
    """Add a new component. Returns error string or None."""
    parent_key = op.get("parent_key")
    component_key = op.get("component_key")
    component_type = op.get("type")

    if not parent_key or not component_key or not component_type:
        return f"add op missing parent_key/component_key/type"
    if parent_key not in comp_def:
        return f"Parent '{parent_key}' not found"
    if component_key in comp_def:
        return f"Component '{component_key}' already exists; use update instead"

    new_comp: dict = {
        "key": component_key,
        "type": component_type,
        "name": component_key,
        "displayOrder": op.get("display_order", 0),
        "children": {},
        "properties": op.get("properties", {}),
        "styleProperties": op.get("style_properties", {}),
    }
    # Set binding paths at top level (not inside properties)
    for bp_key, bp_value in op.get("binding_paths", {}).items():
        new_comp[bp_key] = bp_value

    comp_def[component_key] = new_comp
    comp_def[parent_key].setdefault("children", {})[component_key] = True
    return None


def _op_update(comp_def: dict, op: dict) -> str | None:
    """Merge properties/styles into a component. Returns error string or None."""
    from app.agents.appbuilder.tools.component_tools import _deep_merge

    component_key = op.get("component_key")
    if not component_key:
        return "update op missing component_key"
    if component_key not in comp_def:
        return f"Component '{component_key}' not found"

    comp = comp_def[component_key]
    if op.get("properties"):
        comp.setdefault("properties", {}).update(op["properties"])
    if op.get("style_properties"):
        _deep_merge(comp.setdefault("styleProperties", {}), op["style_properties"])
    if op.get("display_order") is not None:
        comp["displayOrder"] = op["display_order"]
    # Set binding paths at top level (not inside properties)
    for bp_key, bp_value in op.get("binding_paths", {}).items():
        comp[bp_key] = bp_value
    return None


def _op_remove(comp_def: dict, op: dict, root_key: str) -> str | None:
    """Remove a component (and descendants). Returns error string or None."""
    from app.agents.appbuilder.tools.component_tools import _collect_descendants

    component_key = op.get("component_key")
    if not component_key:
        return "remove op missing component_key"
    if component_key not in comp_def:
        return f"Component '{component_key}' not found"
    if component_key == root_key:
        return "Cannot remove the root component"

    keys_to_remove: set[str] = set()
    if op.get("recursive", True):
        _collect_descendants(comp_def, component_key, keys_to_remove)
    keys_to_remove.add(component_key)

    for key in keys_to_remove:
        comp_def.pop(key, None)

    for comp in comp_def.values():
        comp.get("children", {}).pop(component_key, None)

    return None


def _op_move(comp_def: dict, op: dict) -> str | None:
    """Move a component to a new parent. Returns error string or None."""
    component_key = op.get("component_key")
    new_parent_key = op.get("new_parent_key")
    if not component_key or not new_parent_key:
        return "move op missing component_key or new_parent_key"
    if component_key not in comp_def:
        return f"Component '{component_key}' not found"
    if new_parent_key not in comp_def:
        return f"New parent '{new_parent_key}' not found"

    for comp in comp_def.values():
        comp.get("children", {}).pop(component_key, None)

    comp_def[new_parent_key].setdefault("children", {})[component_key] = True

    if op.get("display_order") is not None:
        comp_def[component_key]["displayOrder"] = op["display_order"]

    return None


# ── batch_update_page ────────────────────────────────────────────

async def _batch_update_page_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    page_name = params["page_name"]
    app_code = params.get("app_code", context.get("app_code", ""))
    operations: list[dict] = params.get("operations", [])

    if not operations:
        return ToolResult(success=False, error="No operations provided.")

    page_data, error = await fetch_page_by_name(client, page_name, app_code, headers)
    if error:
        return ToolResult(success=False, error=error)

    comp_def = page_data.setdefault("componentDefinition", {})
    root_key = page_data.get("rootComponent", "")

    errors = []
    applied = []

    for i, op in enumerate(operations):
        op_type = op.get("op")
        err = None

        if op_type == "add":
            err = _op_add(comp_def, op)
        elif op_type == "update":
            err = _op_update(comp_def, op)
        elif op_type == "remove":
            err = _op_remove(comp_def, op, root_key)
        elif op_type == "move":
            err = _op_move(comp_def, op)
        else:
            err = f"Unknown op type '{op_type}' (must be add/update/remove/move)"

        if err:
            errors.append(f"op[{i}] ({op_type}): {err}")
        else:
            key = op.get("component_key", op.get("parent_key", "?"))
            applied.append(f"{op_type} '{key}'")

    if errors and not applied:
        return ToolResult(success=False, error="All operations failed:\n" + "\n".join(errors))

    page_data["message"] = params["message"]
    save_result = await save_page(client, page_data["id"], page_data, headers, context.get("client_code", ""))
    if not save_result.success:
        return save_result

    summary_parts = [f"Applied {len(applied)} operation(s) on page '{page_name}': {', '.join(applied)}."]
    if errors:
        summary_parts.append(f"Skipped {len(errors)} error(s): {'; '.join(errors)}")

    return ToolResult(success=True, summary=" ".join(summary_parts))


batch_update_page = ToolDefinition(
    name="batch_update_page",
    display_name="Batch Update Page",
    description=(
        "Apply multiple component operations to a page in a single fetch+save. "
        "PREFER this over calling add_component/update_component/remove_component/move_component "
        "individually — it makes one API call instead of N. "
        "Each operation is {op, ...fields} where op is one of: "
        "'add' (parent_key, component_key, type, properties?, style_properties?, binding_paths?, display_order?), "
        "'update' (component_key, properties?, style_properties?, binding_paths?, display_order?), "
        "'remove' (component_key, recursive?=true), "
        "'move' (component_key, new_parent_key, display_order?). "
        "\n\nCRITICAL FORMAT RULES:\n"
        "1. properties: EVERY value MUST be a ComponentProperty object, NEVER bare strings. "
        "Static: {\"label\": {\"value\": \"Hello\"}}. "
        "Dynamic: {\"text\": {\"location\": {\"type\": \"EXPRESSION\", \"value\": \"Store.name\"}}}. "
        "Both: {\"text\": {\"value\": \"fallback\", \"location\": {\"type\": \"EXPRESSION\", \"value\": \"Store.name\"}}}. "
        "WRONG: {\"label\": \"Hello\"} (bare string), {\"label\": {\"type\": \"VALUE\", \"value\": \"Hello\"}} (old DataLocation format).\n"
        "2. style_properties: Structure is "
        "{\"<uniqueKey>\": {\"resolutions\": {\"ALL\": {\"<key>\": {\"value\": \"<val>\"}}}}}. "
        "Key format: '<subComponent>-<cssProp>:<pseudoState>' (subComponent/pseudoState optional). "
        "CSS props MUST be camelCase (paddingLeft, marginTop), NEVER shorthand (padding) or kebab-case (padding-left). "
        "Each value MUST be a ComponentProperty object. "
        "WRONG: {\"padding\": ...} (shorthand), {\"padding-left\": ...} (kebab), {\"paddingLeft\": {\"type\": \"VALUE\", \"value\": \"12px\"}} (old format).\n"
        "3. binding_paths: ComponentProperty at TOP LEVEL of component (not inside properties). "
        "Format: {\"bindingPath\": {\"value\": \"Page.storePath\"}}. "
        "Required for: Popup (toggle boolean), TextBox/Dropdown/CheckBox (value path), "
        "ArrayRepeater/Table (data array path), PhoneNumber (number + country + dial), etc."
    ),
    parameters=[
        ToolParameter(name="page_name", type="string", description="Name of the page to modify."),
        ToolParameter(
            name="operations",
            type="array",
            description=(
                "List of operations. Each: "
                "{op:'add', parent_key, component_key, type, properties?, style_properties?, "
                "binding_paths?, display_order?} | "
                "{op:'update', component_key, properties?, style_properties?, binding_paths?, display_order?} | "
                "{op:'remove', component_key, recursive?} | "
                "{op:'move', component_key, new_parent_key, display_order?}. "
                "binding_paths example: {\"bindingPath\": {\"value\": \"Page.isOpen\"}}"
            ),
        ),
        ToolParameter(name="message", type="string", description="Commit message (10–15 words) describing what was changed."),
        ToolParameter(name="app_code", type="string", description="Application code.", required=False),
    ],
    execute=_batch_update_page_execute,
)


BATCH_TOOLS: list[ToolDefinition] = [batch_update_page]
