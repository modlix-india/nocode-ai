"""Kirun page-event functions — inline Kirun on the Page envelope.

Page event functions live in `Page.eventFunctions` (UUID-keyed). Component
events reference them by UUID (e.g. `Button.onClick = "<uuid>"`); the human
`name` is just a label.

Backend endpoints (PageController.java):
  PUT  /api/ui/pages/{id}/events/{key}  body: {definition, expectedEventVersion, message}
  GET  /api/ui/pages/{id}/events/{key}

Per-event optimistic locking: `Page.eventFunctionVersions[key]` is bumped on
each PUT; we pass it back as expectedEventVersion. Use 0 to skip the check
when creating a new event.

Tools exposed (10 total):
  CRUD (4):
    - list_page_event_functions
    - get_page_event_function
    - create_page_event_function
    - delete_page_event_function
  Step ops (4) — same shape as the function step ops, but scoped to a page event:
    - add_event_step
    - update_event_step
    - set_event_step_dependencies
    - remove_event_step
  DSL (2):
    - decompile_page_event_function
    - save_page_event_function_from_text
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

from . import _conventions as c
from . import _kirun_dsl as kirun_dsl
from . import _kirun_layout as kirun_layout
from . import _page_ops as p_ops


# Shared constants — reduce description duplication.
_DESC_APP_CODE = "appCode; defaults to session"
_DESC_PAGE_NAME = "Page that owns the event function"
_DESC_EVENT_REF = "Event function name (the `name` field) or UUID key"
_DESC_COMMIT_MSG = "Commit message"


# ── Helpers ─────────────────────────────────────────────────────────────


def _client_and_headers(context: dict[str, Any]) -> tuple[Any, dict[str, str]]:
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context.get("headers") or {}


def _resolve_app_code(params: dict[str, Any], context: dict[str, Any]) -> tuple[str, ToolResult | None]:
    ac = params.get("app_code") or context.get("app_code", "")
    if not ac:
        return "", ToolResult(success=False, error="No appCode set. Pass `app_code` or set it on the chat request.")
    return ac, None


_PAGE_ONLOAD_ALIASES = frozenset({"onload", "load", "pageload"})


def _normalize_event_alias(s: str) -> str:
    return "".join(ch for ch in s if ch.isalnum()).lower()


def _find_event_by_name_exact(events: dict[str, Any], name: str) -> tuple[str, dict[str, Any]] | None:
    for key, defn in events.items():
        if isinstance(defn, dict) and defn.get("name") == name:
            return key, defn
    return None


def _find_event_by_name_ci(events: dict[str, Any], name: str) -> tuple[str, dict[str, Any]] | None:
    target = name.lower()
    for key, defn in events.items():
        if isinstance(defn, dict):
            actual = defn.get("name")
            if isinstance(actual, str) and actual.lower() == target and actual != name:
                return key, defn
    return None


def _resolve_page_onload_event(page: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """page.properties.onLoadEvent → (key, def) if set + valid."""
    key = (page.get("properties") or {}).get("onLoadEvent")
    if not key:
        return None
    defn = (page.get("eventFunctions") or {}).get(key)
    return (key, defn) if isinstance(defn, dict) else None


async def _resolve_event(
    context: dict[str, Any], page_name: str, app_code: str, event_identifier: str,
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None, str | None]:
    """Return (page, event_key, event_def, error).

    Resolution order:
      1. Direct UUID-key match
      2. Exact name match (case-sensitive)
      3. On-load alias → page.properties.onLoadEvent
      4. Case-insensitive name match
    """
    client, headers = _client_and_headers(context)
    page, err = await p_ops.fetch_page_by_name(client, page_name, app_code, headers)
    if err:
        return None, None, None, err
    assert page is not None
    events = page.get("eventFunctions") or {}

    if event_identifier in events:
        return page, event_identifier, events[event_identifier], None
    hit = _find_event_by_name_exact(events, event_identifier)
    if hit is not None:
        return page, hit[0], hit[1], None
    if _normalize_event_alias(event_identifier) in _PAGE_ONLOAD_ALIASES:
        hit = _resolve_page_onload_event(page)
        if hit is not None:
            return page, hit[0], hit[1], None
    hit = _find_event_by_name_ci(events, event_identifier)
    if hit is not None:
        return page, hit[0], hit[1], None
    return page, None, None, (
        f"event function '{event_identifier}' not found on page '{page_name}'. "
        f"Page has {len(events)} event(s); call list_page_event_functions to see them. "
        f"For the on-load handler, pass 'onLoad' (resolved via properties.onLoadEvent)."
    )


async def _put_event(
    context: dict[str, Any], page_id: str, event_key: str,
    definition: dict[str, Any], expected_version: int, message: str,
) -> tuple[bool, str]:
    client, headers = _client_and_headers(context)
    body = {"definition": definition, "expectedEventVersion": expected_version, "message": message}
    r = await client.put(f"{p_ops.API_PREFIX}/{page_id}/events/{event_key}", headers=headers, json=body)
    if not r.success:
        return False, r.error
    return True, ""


def _build_event_step(
    *, step_name: str, primitive_namespace: str, primitive_name: str,
    params: dict[str, Any] | None, dependencies: list[str] | None,
    position_left: float, position_top: float,
) -> dict[str, Any]:
    dep_map: dict[str, bool] = {}
    for d in dependencies or []:
        if d.startswith("Steps."):
            dep_map[d] = True
        else:
            dep_map[c.make_dependency_key(d, "output")] = True
    return {
        "statementName": step_name,
        "namespace": primitive_namespace,
        "name": primitive_name,
        "position": {"left": position_left, "top": position_top},
        "parameterMap": c.make_parameter_map(params or {}),
        "dependentStatements": dep_map,
    }


# ── list_page_event_functions ────────────────────────────────────────────


async def _execute_list_page_event_functions(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
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
    events = page.get("eventFunctions") or {}
    if not events:
        return ToolResult(success=True, summary=f"Page '{page_name}' has no event functions.")
    versions = page.get("eventFunctionVersions") or {}
    rows = []
    for key, defn in events.items():
        if isinstance(defn, dict):
            rows.append({
                "key": key,
                "name": defn.get("name", "(unnamed)"),
                "steps": len(defn.get("steps") or {}),
                "version": versions.get(key, 1),
            })
    return ToolResult(success=True, summary=f"Event functions on '{page_name}' ({len(rows)}):\n{json.dumps(rows, indent=2, default=str)}")


list_page_event_functions_tool = ToolDefinition(
    name="list_page_event_functions",
    description="List every event function attached to a page. Returns UUID key (use in Button.onClick etc.), name, step count, version.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ],
    execute=_execute_list_page_event_functions,
)


# ── get_page_event_function ──────────────────────────────────────────────


async def _execute_get_page_event_function(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    event = (params.get("event") or "").strip()
    if not page_name or not event:
        return ToolResult(success=False, error="`page_name` and `event` are required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    _page, key, defn, err = await _resolve_event(context, page_name, ac, event)
    if err:
        return ToolResult(success=False, error=err)
    assert defn is not None and key is not None
    include = (params.get("include") or "summary").strip()
    if include == "full":
        return ToolResult(success=True, summary=f"key: {key}\n{json.dumps(defn, indent=2, default=str)}")
    steps = defn.get("steps") or {}
    lines = [
        f"Event function on page '{page_name}':",
        f"  key: {key}  (use in Button.onClick etc.)",
        f"  name: {defn.get('name', '(unnamed)')}",
        f"  steps: {len(steps)}",
    ]
    for step_name, step in steps.items():
        ns = step.get("namespace", "?")
        primitive = step.get("name", "?")
        deps = c.active_dependencies(step.get("dependentStatements"))
        param_names = list((step.get("parameterMap") or {}).keys())
        extra: list[str] = []
        if deps:
            extra.append(f"after [{', '.join(sorted(deps))}]")
        if param_names:
            extra.append(f"params [{', '.join(param_names)}]")
        suffix = (" — " + "; ".join(extra)) if extra else ""
        lines.append(f"  - {step_name}: {ns}.{primitive}{suffix}")
    return ToolResult(success=True, summary="\n".join(lines))


get_page_event_function_tool = ToolDefinition(
    name="get_page_event_function",
    description="Read one event function from a page. Accepts the human name OR UUID key. include='summary' (default: steps + deps) or 'full'.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name"),
        ToolParameter(name="event", type="string", description=_DESC_EVENT_REF),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="include", type="string", required=False, default="summary", description="summary | full"),
    ],
    execute=_execute_get_page_event_function,
)


# ── create_page_event_function ───────────────────────────────────────────


async def _execute_create_page_event_function(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    event_name = (params.get("event_name") or "").strip()
    if not page_name or not event_name:
        return ToolResult(success=False, error="`page_name` and `event_name` are required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    page, err = await p_ops.fetch_page_by_name(client, page_name, ac, headers)
    if err:
        return ToolResult(success=False, error=err)
    assert page is not None
    events = page.get("eventFunctions") or {}
    for k, defn in events.items():
        if isinstance(defn, dict) and defn.get("name") == event_name:
            return ToolResult(success=False, error=f"Event function named '{event_name}' already exists (key={k}).")
    new_key = uuid.uuid4().hex
    steps = params.get("steps") or {}
    definition = {"name": event_name, "steps": steps}
    # Auto-layout any provided steps.
    if steps:
        kirun_layout.auto_layout_steps(definition["steps"])
    ok, err = await _put_event(
        context, page.get("id"), new_key, definition,
        expected_version=0,
        message=params.get("message") or "Created event function via CFA",
    )
    if not ok:
        return ToolResult(success=False, error=err)
    return ToolResult(success=True, summary=f"Created event function '{event_name}' on page '{page_name}'.\n  key: {new_key}\n  Use this key in component event props (e.g. Button.onClick).")


create_page_event_function_tool = ToolDefinition(
    name="create_page_event_function",
    description="Create a new event function on a page. Returns the UUID key to wire into component event props.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page to attach the event function to"),
        ToolParameter(name="event_name", type="string", description="Human name (e.g. 'send_form', 'onLoad')"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="steps", type="object", required=False, description="Optional pre-built steps map (usually empty; use add_event_step)"),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG),
    ],
    execute=_execute_create_page_event_function,
)


# ── delete_page_event_function ───────────────────────────────────────────


async def _execute_delete_page_event_function(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    event = (params.get("event") or "").strip()
    if not page_name or not event:
        return ToolResult(success=False, error="`page_name` and `event` are required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    page, key, _defn, err = await _resolve_event(context, page_name, ac, event)
    if err:
        return ToolResult(success=False, error=err)
    assert page is not None and key is not None
    events = dict(page.get("eventFunctions") or {})
    events.pop(key, None)
    page["eventFunctions"] = events
    page["message"] = params.get("message") or "Removed event function via CFA"
    client, headers = _client_and_headers(context)
    r = await client.put(f"{p_ops.API_PREFIX}/{page.get('id')}", headers=headers, json=page)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Removed event function '{event}' (key={key}) from page '{page_name}'.")


delete_page_event_function_tool = ToolDefinition(
    name="delete_page_event_function",
    description="Remove an event function from a page (full-doc PUT — no surgical DELETE endpoint). Components referencing the removed key silently no-op at runtime; clear those bindings first.",
    parameters=[
        ToolParameter(name="page_name", type="string", description=_DESC_PAGE_NAME),
        ToolParameter(name="event", type="string", description=_DESC_EVENT_REF),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG),
    ],
    execute=_execute_delete_page_event_function,
)


# ── add_event_step ───────────────────────────────────────────────────────


async def _execute_add_event_step(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    event = (params.get("event") or "").strip()
    step_name = (params.get("step_name") or "").strip()
    primitive_namespace = (params.get("primitive_namespace") or "").strip()
    primitive_name = (params.get("primitive_name") or "").strip()
    if not all([page_name, event, step_name, primitive_namespace, primitive_name]):
        return ToolResult(success=False, error="`page_name`, `event`, `step_name`, `primitive_namespace`, `primitive_name` are required")
    name_err = c.validate_simple_name(step_name)
    if name_err:
        return ToolResult(success=False, error=f"step_name — {name_err}")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    page, key, defn, err = await _resolve_event(context, page_name, ac, event)
    if err:
        return ToolResult(success=False, error=err)
    assert page is not None and key is not None and defn is not None
    steps = dict(defn.get("steps") or {})
    if step_name in steps:
        return ToolResult(success=False, error=f"step '{step_name}' already exists in event '{event}'. Use update_event_step.")

    warning = c.validate_step_call(primitive_namespace, primitive_name)
    steps[step_name] = _build_event_step(
        step_name=step_name,
        primitive_namespace=primitive_namespace,
        primitive_name=primitive_name,
        params=params.get("params"),
        dependencies=params.get("dependencies"),
        position_left=float(params.get("position_left") or 0),
        position_top=float(params.get("position_top") or 0),
    )
    new_defn = dict(defn)
    new_defn["steps"] = steps
    ok, err = await _put_event(
        context, page.get("id"), key, new_defn,
        expected_version=c.event_function_version_for(page, key),
        message=params.get("message") or "Added event step via CFA",
    )
    if not ok:
        return ToolResult(success=False, error=err)
    msg = f"Added step '{step_name}' ({primitive_namespace}.{primitive_name}) to event '{event}' on '{page_name}'."
    if warning:
        msg += f"\nWarning: {warning}"
    return ToolResult(success=True, summary=msg)


add_event_step_tool = ToolDefinition(
    name="add_event_step",
    description="Add a step to a page event function (surgical, optimistic-locked). Same dependency / parameter rules as add_step.",
    parameters=[
        ToolParameter(name="page_name", type="string", description=_DESC_PAGE_NAME),
        ToolParameter(name="event", type="string", description=_DESC_EVENT_REF),
        ToolParameter(name="step_name", type="string", description="Unique step name within the event function"),
        ToolParameter(name="primitive_namespace", type="string", description="e.g. 'UIEngine', 'System.Math', 'MyApp'"),
        ToolParameter(name="primitive_name", type="string", description="Primitive name"),
        ToolParameter(name="params", type="object", required=False, description="Parameter values (Steps./Page./etc. auto-coerce to expressions)"),
        ToolParameter(name="dependencies", type="array", required=False, description="'stepName' or 'Steps.<step>.<event>'", items={"type": "string"}),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="position_left", type="number", required=False, default=0, description="Editor x"),
        ToolParameter(name="position_top", type="number", required=False, default=0, description="Editor y"),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG),
    ],
    execute=_execute_add_event_step,
)


# ── update_event_step ────────────────────────────────────────────────────


async def _execute_update_event_step(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    event = (params.get("event") or "").strip()
    step_name = (params.get("step_name") or "").strip()
    if not page_name or not event or not step_name:
        return ToolResult(success=False, error="`page_name`, `event`, `step_name` are required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    page, key, defn, err = await _resolve_event(context, page_name, ac, event)
    if err:
        return ToolResult(success=False, error=err)
    assert page is not None and key is not None and defn is not None
    steps = dict(defn.get("steps") or {})
    if step_name not in steps:
        return ToolResult(success=False, error=f"step '{step_name}' not found in event '{event}'.")

    statement = dict(steps[step_name])
    changed: list[str] = []
    if params.get("primitive_namespace") is not None:
        statement["namespace"] = params["primitive_namespace"]
        changed.append("namespace")
    if params.get("primitive_name") is not None:
        statement["name"] = params["primitive_name"]
        changed.append("primitive")
    if params.get("params") is not None:
        statement["parameterMap"] = c.make_parameter_map(params["params"])
        changed.append("parameterMap")
    if params.get("position_left") is not None or params.get("position_top") is not None:
        pos = dict(statement.get("position") or {})
        if params.get("position_left") is not None:
            pos["left"] = float(params["position_left"])
        if params.get("position_top") is not None:
            pos["top"] = float(params["position_top"])
        statement["position"] = pos
        changed.append("position")
    if not changed:
        return ToolResult(success=True, summary="No-op: nothing to update.")

    steps[step_name] = statement
    new_defn = dict(defn)
    new_defn["steps"] = steps
    ok, err = await _put_event(
        context, page.get("id"), key, new_defn,
        expected_version=c.event_function_version_for(page, key),
        message=params.get("message") or "Updated event step via CFA",
    )
    if not ok:
        return ToolResult(success=False, error=err)
    return ToolResult(success=True, summary=f"Updated step '{step_name}' in event '{event}': {', '.join(changed)}.")


update_event_step_tool = ToolDefinition(
    name="update_event_step",
    description="Replace fields on a step within a page event function. params REPLACES the full parameterMap.",
    parameters=[
        ToolParameter(name="page_name", type="string", description=_DESC_PAGE_NAME),
        ToolParameter(name="event", type="string", description=_DESC_EVENT_REF),
        ToolParameter(name="step_name", type="string", description="Step to update"),
        ToolParameter(name="params", type="object", required=False, description="REPLACES the full parameterMap"),
        ToolParameter(name="primitive_namespace", type="string", required=False, description="New namespace"),
        ToolParameter(name="primitive_name", type="string", required=False, description="New primitive"),
        ToolParameter(name="position_left", type="number", required=False, description="Editor x"),
        ToolParameter(name="position_top", type="number", required=False, description="Editor y"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG),
    ],
    execute=_execute_update_event_step,
)


# ── set_event_step_dependencies ──────────────────────────────────────────


async def _execute_set_event_step_dependencies(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    event = (params.get("event") or "").strip()
    step_name = (params.get("step_name") or "").strip()
    depends_on = params.get("depends_on")
    if not page_name or not event or not step_name or not isinstance(depends_on, list):
        return ToolResult(success=False, error="`page_name`, `event`, `step_name`, `depends_on` (list) are required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    page, key, defn, err = await _resolve_event(context, page_name, ac, event)
    if err:
        return ToolResult(success=False, error=err)
    assert page is not None and key is not None and defn is not None
    steps = dict(defn.get("steps") or {})
    if step_name not in steps:
        return ToolResult(success=False, error=f"step '{step_name}' not found in event '{event}'.")
    dep_map: dict[str, bool] = {}
    for d in depends_on:
        if d.startswith("Steps."):
            dep_map[d] = True
        else:
            dep_map[c.make_dependency_key(d, "output")] = True
    statement = dict(steps[step_name])
    statement["dependentStatements"] = dep_map
    steps[step_name] = statement
    new_defn = dict(defn)
    new_defn["steps"] = steps
    ok, err = await _put_event(
        context, page.get("id"), key, new_defn,
        expected_version=c.event_function_version_for(page, key),
        message=params.get("message") or "Updated event step deps via CFA",
    )
    if not ok:
        return ToolResult(success=False, error=err)
    return ToolResult(success=True, summary=f"Set dependencies on '{step_name}' in event '{event}': {list(dep_map.keys()) or '(none)'}.")


set_event_step_dependencies_tool = ToolDefinition(
    name="set_event_step_dependencies",
    description="Replace a step's dependentStatements within a page event function. Pass [] to clear.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name"),
        ToolParameter(name="event", type="string", description=_DESC_EVENT_REF),
        ToolParameter(name="step_name", type="string", description="Step whose dependencies to set"),
        ToolParameter(name="depends_on", type="array", description="'stepName' or 'Steps.<step>.<event>'", items={"type": "string"}),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG),
    ],
    execute=_execute_set_event_step_dependencies,
)


# ── remove_event_step ────────────────────────────────────────────────────


def _find_event_dangling_refs(steps: dict[str, Any], removed: str) -> list[str]:
    out: list[str] = []
    for other_name, other in steps.items():
        if other_name == removed:
            continue
        for dep in c.active_dependencies(other.get("dependentStatements")):
            if dep == removed or dep.startswith(f"Steps.{removed}."):
                out.append(f"{other_name} (dep on '{dep}')")
                break
        for param_refs in (other.get("parameterMap") or {}).values():
            for ref in param_refs.values():
                if ref.get("type") == "EXPRESSION" and removed in c.steps_referenced(ref.get("expression", "")):
                    out.append(f"{other_name} (expr ref to Steps.{removed})")
                    break
    return out


async def _execute_remove_event_step(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    event = (params.get("event") or "").strip()
    step_name = (params.get("step_name") or "").strip()
    if not page_name or not event or not step_name:
        return ToolResult(success=False, error="`page_name`, `event`, `step_name` are required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    page, key, defn, err = await _resolve_event(context, page_name, ac, event)
    if err:
        return ToolResult(success=False, error=err)
    assert page is not None and key is not None and defn is not None
    steps = dict(defn.get("steps") or {})
    if step_name not in steps:
        return ToolResult(success=False, error=f"step '{step_name}' not found in event '{event}'.")
    dangling = _find_event_dangling_refs(steps, step_name)
    steps.pop(step_name)
    new_defn = dict(defn)
    new_defn["steps"] = steps
    ok, err = await _put_event(
        context, page.get("id"), key, new_defn,
        expected_version=c.event_function_version_for(page, key),
        message=params.get("message") or "Removed event step via CFA",
    )
    if not ok:
        return ToolResult(success=False, error=err)
    msg = f"Removed step '{step_name}' from event '{event}' on page '{page_name}'."
    if dangling:
        msg += "\nWARNING: dangling refs in other steps:\n  - " + "\n  - ".join(dangling)
    return ToolResult(success=True, summary=msg)


remove_event_step_tool = ToolDefinition(
    name="remove_event_step",
    description="Remove a step from a page event function. Warns about other steps that still reference its outputs (explicit deps OR expression refs).",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page"),
        ToolParameter(name="event", type="string", description=_DESC_EVENT_REF),
        ToolParameter(name="step_name", type="string", description="Step to remove"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG),
    ],
    execute=_execute_remove_event_step,
)


# ── decompile_page_event_function ────────────────────────────────────────


async def _execute_decompile_page_event_function(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    event = (params.get("event") or "").strip()
    if not page_name or not event:
        return ToolResult(success=False, error="`page_name` and `event` are required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    _page, _key, defn, err = await _resolve_event(context, page_name, ac, event)
    if err:
        return ToolResult(success=False, error=err)
    assert defn is not None
    try:
        text = await kirun_dsl.decompile_json(defn)
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"Decompile error: {type(e).__name__}: {e}")
    return ToolResult(success=True, summary=text)


decompile_page_event_function_tool = ToolDefinition(
    name="decompile_page_event_function",
    description="Fetch a page event function and return its DSL text. The decompiler synthesizes a minimal NAMESPACE/PARAMETERS/EVENTS wrapper since inline event functions only have name + steps.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name"),
        ToolParameter(name="event", type="string", description=_DESC_EVENT_REF),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ],
    execute=_execute_decompile_page_event_function,
)


# ── save_page_event_function_from_text ───────────────────────────────────


def _find_unresolved_inline_expressions(steps: dict[str, Any]) -> list[str]:
    """Find per-field expressions buried inside an object-literal parameter.

    The DSL compiler encodes an expression written INSIDE an object literal as
    `{"isExpression": true, "value": "<expr>"}`. Only a parameter declared
    `Parameter.EXPRESSION` in its function signature ever evaluates that shape,
    and the sole one in kirun is System.GenerateEvent's `results.value`.
    Everywhere else - notably UIEngine.SendData's `payload`, which is
    `Schema.ofAny` and goes straight to axios - the marker object is sent
    verbatim, so the request body carries `{"isExpression": true, ...}` per
    field and the server rejects it against the storage schema.

    Real apps never do this: a SendData payload is ONE `type: EXPRESSION` entry
    naming a single page path (verified across the leadzump/appbuilder pages).
    Returns dotted locations of each offending field.
    """
    hits: list[str] = []

    def walk(node: Any, path: str, inside_param: bool) -> None:
        if isinstance(node, dict):
            if inside_param and node.get("isExpression") is True:
                hits.append(f"{path} = {node.get('value')!r}")
                return
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k), inside_param or k == "parameterMap")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]", inside_param)

    for step_name, step in (steps or {}).items():
        if not isinstance(step, dict):
            continue
        # System.GenerateEvent is the ONE function whose parameter is declared
        # Parameter.EXPRESSION (GenerateEvent.ts results.value), so the marker
        # shape is correct there and is exactly how every real server function
        # returns a computed result. Flagging it would reject valid code.
        if step.get("namespace") == "System" and step.get("name") == "GenerateEvent":
            continue
        walk(step, str(step_name), False)
    return hits


def _inline_expression_error(hits: list[str]) -> str:
    shown = "\n".join(f"  - {h}" for h in hits[:12])
    more = f"\n  ...(+{len(hits) - 12} more)" if len(hits) > 12 else ""
    return (
        f"{len(hits)} expression(s) are written inside an object literal, where they are "
        f"NEVER evaluated:\n{shown}{more}\n\n"
        "The compiler encodes those as {\"isExpression\": true, \"value\": \"...\"} and only "
        "System.GenerateEvent evaluates that shape. UIEngine.SendData sends the payload "
        "as-is, so the request body would contain those marker objects instead of values "
        "and the server would reject it against the storage schema. Nothing was saved.\n\n"
        "Build the object in page state first, then send it as ONE expression:\n"
        "  - bind the form inputs to fields of a single object, e.g. Page.newGroup.name "
        "and Page.newGroup.monthlyInstallment (set bindingPath on each input);\n"
        "  - then: CoreServices.Storage.Create(storageName=\"chitgroup\", dataObject=Page.newGroup)\n"
        "That is the shape every working app uses: payload is a single expression naming "
        "one path, not an object literal with a computed value per field.\n\n"
        "A storage write is a KIRun BLOCK used as a STEP, never an HTTP call: pages call "
        "CoreServices.Storage.Create/Update/ReadPage directly as steps. api/core/data/... "
        "and SendData to api/core/function/execute/... are both wrong for data."
    )


async def _execute_save_page_event_function_from_text(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    event_name = (params.get("event_name") or "").strip()
    text = params.get("text") or ""
    if not page_name or not event_name or not text:
        return ToolResult(success=False, error="`page_name`, `event_name`, `text` are required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    try:
        compiled = kirun_dsl.compile_text(text)
    except Exception as e:  # noqa: BLE001
        # Same hint layer compile_kirun_text / save_function_from_text use.
        # This path returned the bare parser error, which is how the Chit
        # Fund run burned two turns on one operator-in-argument mistake.
        from .kirun import _hint_for_compile_error  # lazy: kirun.py does not import this module
        raw = f"{type(e).__name__}: {e}"
        return ToolResult(success=False, error=f"Compile error: {raw}\n\n{_hint_for_compile_error(raw)}")
    dead_expr = _find_unresolved_inline_expressions(compiled.get("steps") or {})
    if dead_expr:
        return ToolResult(success=False, error=_inline_expression_error(dead_expr))
    inline_defn = {
        "name": event_name,
        "steps": compiled.get("steps") or {},
    }
    kirun_layout.auto_layout_steps(inline_defn["steps"])
    client, headers = _client_and_headers(context)
    page, err = await p_ops.fetch_page_by_name(client, page_name, ac, headers)
    if err:
        return ToolResult(success=False, error=err)
    assert page is not None
    events = page.get("eventFunctions") or {}
    key: str | None = None
    for k, defn in events.items():
        if isinstance(defn, dict) and defn.get("name") == event_name:
            key = k
            break
    is_new = key is None
    if is_new:
        key = uuid.uuid4().hex
    versions = page.get("eventFunctionVersions") or {}
    expected = 0 if is_new else int(versions.get(key, 1))
    ok, err = await _put_event(
        context, page.get("id"), key, inline_defn, expected,
        params.get("message") or "Saved page event from DSL via CFA",
    )
    if not ok:
        return ToolResult(success=False, error=err)
    verb = "Created" if is_new else "Updated"
    # Spell out the key-not-name rule here: the runtime resolves event props by
    # map key (`pageDefinition.eventFunctions[onClick]`), so wiring a button to
    # '<event_name>' produces a handler that never fires. A whole generated app
    # once shipped with all 22 handlers dead because this line only printed the
    # key without saying what to do with it.
    wiring = (
        f"\n  key: {key}"
        f"\n  Wire it with the KEY, not the name '{event_name}':"
        f"\n    onClick={key!r}   (add_component / patch_component_props)"
    )
    if event_name.lower() in ("onload", "on_load", "pageload"):
        wiring += (
            f"\n  On-load handlers only run when the PAGE points at them: set "
            f"page properties.onLoadEvent={key!r} (update_page), otherwise this "
            f"function never executes and the page fetches nothing."
        )
    return ToolResult(
        success=True,
        summary=f"{verb} event function '{event_name}' on page '{page_name}'.{wiring}",
    )


save_page_event_function_from_text_tool = ToolDefinition(
    name="save_page_event_function_from_text",
    description="""Compile DSL and save as a page event function. Create-or-update by `event_name` — if a function with that name exists on the page, update it; otherwise create.

Use this to wire button onClick, page onLoad, TextBox onChange, etc. Page event functions live on a page's `eventFunctions` map (UUID-keyed). The flow is:
1. `save_page_event_function_from_text(page_name="login", event_name="handleSignIn", text="<DSL>")` — author + save in one shot.
2. `patch_component_props(page_name="login", component_key="signInBtn", properties={"onClick": {"value": "handleSignIn"}})` — wire it onto the button.

The DSL shape is the same as a regular Kirun function (FUNCTION / NAMESPACE / PARAMETERS / EVENTS / LOGIC) — but page event functions cannot receive Arguments. They read from Store / Page / Parent contexts instead. The top-level FUNCTION/NAMESPACE/PARAMETERS/EVENTS in the DSL are ignored for inline use; only the LOGIC steps matter.

Example DSL for an event that saves a storage row and toasts a message:
```
FUNCTION saveMember
    NAMESPACE _
    LOGIC
        save: CoreServices.Storage.Create(storageName = "member", dataObject = Page.member)
            output
                toast: UIEngine.Message(msg = "Member saved", type = "SUCCESS") AFTER Steps.save.output
            error
                fail: UIEngine.Message(msg = "Could not save", type = "ERROR") AFTER Steps.save.error
```

Common pitfalls:
- Page events cannot use `Arguments.X` — those don't exist. Use `Page.X` / `Store.X` / `Parent.X` instead.
- The function's NAME in the DSL (`FUNCTION saveMember`) must match the `event_name` param.
- Use `UIEngine.*` primitives for UI-side calls: FetchData (GET), SendData (POST/PUT), DeleteData, SetStore, Navigate, Message, Login, Logout, CopyTextToClipboard. NOT `System.*` ones (those are server-side). There is NO UIEngine.HTTPRequest / Toast / Read / Create / Update / Delete — `get_kirun_primitive(namespace="UIEngine", name=...)` lists the real set.
- An argument value that starts with a double-quoted string and continues with an operator must be parenthesised: `path = ("Store.x." + Parent.id)`.""",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page to attach the event function to"),
        ToolParameter(name="event_name", type="string", description="Event function name (matches the inline `name` field)"),
        ToolParameter(name="text", type="string", description="DSL source. LOGIC block becomes the event's steps."),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG),
    ],
    execute=_execute_save_page_event_function_from_text,
)


# ── Module export ────────────────────────────────────────────────────────


TOOLS: list[ToolDefinition] = [
    list_page_event_functions_tool,
    get_page_event_function_tool,
    create_page_event_function_tool,
    delete_page_event_function_tool,
    add_event_step_tool,
    update_event_step_tool,
    set_event_step_dependencies_tool,
    remove_event_step_tool,
    decompile_page_event_function_tool,
    save_page_event_function_from_text_tool,
]
