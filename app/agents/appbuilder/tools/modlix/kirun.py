"""Kirun authoring tools — functions, server functions, page-event functions,
DSL compile/decompile, primitives discovery, execution.

Consolidates the modlix-mcp Kirun surface:
  modlix-mcp/modlix_mcp/tools/functions.py            → 5 UI function CRUD tools
  modlix-mcp/modlix_mcp/tools/server_functions.py     → 5 server function CRUD tools
  modlix-mcp/modlix_mcp/tools/page_event_functions.py → 8 page-event tools (CRUD + step ops)
  modlix-mcp/modlix_mcp/tools/function_steps.py       → 4 function step ops
  modlix-mcp/modlix_mcp/tools/function_execute.py     → 1 execute_function
  modlix-mcp/modlix_mcp/tools/kirun_dsl_tools.py      → 7 DSL tools (compile/validate/
                                                        format/decompile/save_from_text)
  modlix-mcp/modlix_mcp/tools/kirun_primitives.py     → 2 primitive discovery tools

Auth: every tool reads JWT from `context["headers"]` set by the agent loop
from the caller's request. No separate dev login.

Auto-layout: every save_*_from_text tool runs `auto_layout_definition(defn)`
before sending, so the visual editor opens onto a readable DAG (algorithm
matches `nocode-kirun/kirun-ui/src/util/autoLayout.ts`).
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

from . import _conventions as c
from . import _kirun_dsl as kirun_dsl
from . import _kirun_layout as kirun_layout
from . import _page_ops as p_ops


# ── Common helpers ───────────────────────────────────────────────────────


_UI_FN_API = "/api/ui/functions"
_CORE_FN_API = "/api/core/functions"

# Recurring ToolParameter descriptions — extracted to constants because every
# CRUD-shaped tool in this module shares them. The linter complained about
# 17× duplication of "appCode; defaults to session" alone.
_DESC_APP_CODE = "appCode; defaults to session"
_DESC_CLIENT_CODE = "clientCode; defaults to session"
_DESC_COMMIT_MSG = "Commit message"
_DESC_FN_NAME = "Full function name (Namespace.LocalName)"
_DESC_FN_NAME_SHORT = "Full function name"
_DESC_TARGET_SERVER = "Target server (core)"
_DESC_TARGET_SERVER_FN = "True → operate on a server (core) function. False → UI function (default)."


def _fn_api(is_server: bool) -> str:
    return _CORE_FN_API if is_server else _UI_FN_API


def _fn_kind(is_server: bool) -> str:
    return "server function" if is_server else "UI function"


def _client_and_headers(context: dict[str, Any]) -> tuple[Any, dict[str, str]]:
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context.get("headers") or {}


def _resolve_app_code(params: dict[str, Any], context: dict[str, Any]) -> tuple[str, ToolResult | None]:
    ac = params.get("app_code") or context.get("app_code", "")
    if not ac:
        return "", ToolResult(
            success=False,
            error="No appCode set. Pass `app_code` or set it on the chat request.",
        )
    return ac, None


def _resolve_client_code(params: dict[str, Any], context: dict[str, Any]) -> str:
    return params.get("client_code") or context.get("client_code", "") or ""


async def _fetch_function_by_name(
    client: Any, api: str, headers: dict[str, str], app_code: str, name: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """List by name, then GET detail by id. Same pattern as page fetch."""
    r = await client.get(api, headers=headers, params={"page": 0, "size": 1, "appCode": app_code, "name": name})
    if not r.success:
        return None, r.error
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    if not content:
        return None, f"function '{name}' not found in app '{app_code}'"
    fn_id = content[0].get("id")
    if not fn_id:
        return None, f"function '{name}' has no id"
    detail = await client.get(f"{api}/{fn_id}", headers=headers)
    if not detail.success:
        return None, detail.error
    return detail.data if isinstance(detail.data, dict) else {}, None


# ── Function CRUD (UI + server, parametrized by is_server flag) ──────────


async def _execute_list_functions_for(
    params: dict[str, Any], context: dict[str, Any], is_server: bool,
) -> ToolResult:
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    try:
        size = max(1, min(int(params.get("size") or 200), 1000))
    except (TypeError, ValueError):
        size = 200
    namespace = params.get("namespace")
    client, headers = _client_and_headers(context)
    api = _fn_api(is_server)
    request_params: dict[str, Any] = {"page": 0, "size": size, "appCode": ac}
    if namespace:
        request_params["namespace"] = namespace
    r = await client.get(api, headers=headers, params=request_params)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    rows = [{
        "name": x.get("name"),
        "id": x.get("id"),
        "version": x.get("version"),
        "clientCode": x.get("clientCode"),
    } for x in content]
    return ToolResult(
        success=True,
        summary=f"{_fn_kind(is_server)}s in app '{ac}' ({len(rows)}):\n{json.dumps(rows, indent=2, default=str)}",
    )


async def _execute_list_functions(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    return await _execute_list_functions_for(params, context, is_server=False)


async def _execute_list_server_functions(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    return await _execute_list_functions_for(params, context, is_server=True)


list_functions_tool = ToolDefinition(
    name="list_functions",
    description="List UI (browser-side) Kirun functions in an app. Returns name + id + version.",
    parameters=[
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="namespace", type="string", required=False, description="Filter by namespace (e.g. 'TestUI')"),
        ToolParameter(name="size", type="integer", required=False, default=200, description="Max rows (capped at 1000)"),
    ],
    execute=_execute_list_functions,
)


list_server_functions_tool = ToolDefinition(
    name="list_server_functions",
    description="List server-side (core) Kirun functions in an app. Runs on the Java runtime; can use Database/HTTP/Storage primitives.",
    parameters=[
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="namespace", type="string", required=False, description="Filter by namespace"),
        ToolParameter(name="size", type="integer", required=False, default=200, description="Max rows"),
    ],
    execute=_execute_list_server_functions,
)


async def _execute_get_function_for(
    params: dict[str, Any], context: dict[str, Any], is_server: bool,
) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    fn, err = await _fetch_function_by_name(client, _fn_api(is_server), headers, ac, name)
    if err:
        return ToolResult(success=False, error=err)
    assert fn is not None
    include = (params.get("include") or "summary").strip()
    if include == "full":
        return ToolResult(success=True, summary=json.dumps(fn, indent=2, default=str))
    # summary
    defn = fn.get("definition") or {}
    summary = {
        "id": fn.get("id"),
        "name": fn.get("name"),
        "namespace": defn.get("namespace") or fn.get("namespace"),
        "version": fn.get("version"),
        "clientCode": fn.get("clientCode"),
        "stepCount": len(defn.get("steps") or {}),
        "events": list((defn.get("events") or {}).keys()),
        "parameters": [p.get("parameterName") for p in (defn.get("parameters") or [])],
    }
    return ToolResult(success=True, summary=json.dumps(summary, indent=2, default=str))


async def _execute_get_function(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    return await _execute_get_function_for(params, context, is_server=False)


async def _execute_get_server_function(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    return await _execute_get_function_for(params, context, is_server=True)


get_function_tool = ToolDefinition(
    name="get_function",
    description="Read a UI function. include='summary' (default: counts + events + params) or 'full' (entire JSON, can be huge).",
    parameters=[
        ToolParameter(name="name", type="string", description="Function name (Namespace.LocalName or just LocalName)"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="include", type="string", required=False, default="summary", description="summary | full"),
    ],
    execute=_execute_get_function,
)


get_server_function_tool = ToolDefinition(
    name="get_server_function",
    description="Read a server (core) function. Same shape as get_function.",
    parameters=[
        ToolParameter(name="name", type="string", description="Function name"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="include", type="string", required=False, default="summary", description="summary | full"),
    ],
    execute=_execute_get_server_function,
)


def _validate_function_create(name: str, namespace: str) -> str | None:
    err = c.validate_simple_name(name)
    if err:
        return err
    if namespace == "UIEngine" or namespace.startswith("System"):
        return f"Namespace '{namespace}' is reserved. Use an app-specific namespace."
    ne = c.validate_namespaced_name(namespace)
    if ne:
        return f"namespace — {ne}"
    return None


async def _execute_create_function_for(
    params: dict[str, Any], context: dict[str, Any], is_server: bool,
) -> ToolResult:
    name = (params.get("name") or "").strip()
    namespace = (params.get("namespace") or "").strip()
    err = _validate_function_create(name, namespace)
    if err:
        return ToolResult(success=False, error=err)
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    cc = _resolve_client_code(params, context)
    definition = params.get("definition") or {
        "namespace": namespace, "name": name, "steps": {}, "events": {}, "parameters": [],
    }
    # Ensure namespace+name are on the body
    definition.setdefault("namespace", namespace)
    definition.setdefault("name", name)
    # Auto-layout for any steps the caller passed.
    kirun_layout.auto_layout_definition(definition)
    full_name = f"{namespace}.{name}"
    envelope = {
        "name": full_name, "appCode": ac, "clientCode": cc,
        "message": params.get("message") or f"Created {_fn_kind(is_server)} via CFA",
        "definition": definition,
    }
    client, headers = _client_and_headers(context)
    r = await client.post(_fn_api(is_server), headers=headers, json=envelope)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    fid = (r.data or {}).get("id", "?") if isinstance(r.data, dict) else "?"
    return ToolResult(success=True, summary=f"Created {_fn_kind(is_server)} '{full_name}' (id={fid}).")


async def _execute_create_function(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    return await _execute_create_function_for(params, context, is_server=False)


async def _execute_create_server_function(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    return await _execute_create_function_for(params, context, is_server=True)


_create_function_params = [
    ToolParameter(name="name", type="string", description="Function local name (letters/digits)"),
    ToolParameter(name="namespace", type="string", description="App-specific namespace (NOT 'System' / 'UIEngine')"),
    ToolParameter(name="definition", type="object", required=False, description="Optional full function definition (steps/events/parameters). Defaults to an empty function."),
    ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ToolParameter(name="client_code", type="string", required=False, description="Owning clientCode"),
    ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG),
]

create_function_tool = ToolDefinition(
    name="create_function",
    description="Create a UI (browser-side) Kirun function. Use the DSL alternative `save_function_from_text` for readable authoring; this raw form accepts a JSON definition.",
    parameters=list(_create_function_params),
    execute=_execute_create_function,
)


create_server_function_tool = ToolDefinition(
    name="create_server_function",
    description="Create a server (core) Kirun function. Same shape as create_function but the core runtime gets backend-only primitives (Database/HTTP/Storage).",
    parameters=list(_create_function_params),
    execute=_execute_create_server_function,
)


async def _execute_update_function_for(
    params: dict[str, Any], context: dict[str, Any], is_server: bool,
) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required (full dotted Namespace.LocalName)")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    api = _fn_api(is_server)
    existing, err = await _fetch_function_by_name(client, api, headers, ac, name)
    if err:
        return ToolResult(success=False, error=err)
    assert existing is not None
    fn_id = existing.get("id")
    if not fn_id:
        return ToolResult(success=False, error=f"{_fn_kind(is_server)} '{name}' has no id")
    definition = params.get("definition")
    if definition is not None:
        if not isinstance(definition, dict):
            return ToolResult(success=False, error="`definition` must be a dict")
        kirun_layout.auto_layout_definition(definition)
        existing["definition"] = definition
    existing["message"] = params.get("message") or f"Updated {_fn_kind(is_server)} via CFA"
    r = await client.put(f"{api}/{fn_id}", headers=headers, json=existing)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Updated {_fn_kind(is_server)} '{name}'.")


async def _execute_update_function(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    return await _execute_update_function_for(params, context, is_server=False)


async def _execute_update_server_function(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    return await _execute_update_function_for(params, context, is_server=True)


_update_function_params = [
    ToolParameter(name="name", type="string", description="Function name to update"),
    ToolParameter(name="definition", type="object", required=False, description="Replacement definition (steps/events/parameters). Omit to update metadata only."),
    ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG),
]

update_function_tool = ToolDefinition(
    name="update_function",
    description="Update a UI function — replace its definition.",
    parameters=list(_update_function_params),
    execute=_execute_update_function,
)


update_server_function_tool = ToolDefinition(
    name="update_server_function",
    description="Update a server (core) function — replace its definition.",
    parameters=list(_update_function_params),
    execute=_execute_update_server_function,
)


async def _execute_delete_function_for(
    params: dict[str, Any], context: dict[str, Any], is_server: bool,
) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    api = _fn_api(is_server)
    existing, err = await _fetch_function_by_name(client, api, headers, ac, name)
    if err:
        return ToolResult(success=False, error=err)
    fn_id = (existing or {}).get("id")
    if not fn_id:
        return ToolResult(success=False, error=f"{_fn_kind(is_server)} '{name}' has no id")
    r = await client.delete(f"{api}/{fn_id}", headers=headers)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Deleted {_fn_kind(is_server)} '{name}' (id={fn_id}).")


async def _execute_delete_function(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    return await _execute_delete_function_for(params, context, is_server=False)


async def _execute_delete_server_function(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    return await _execute_delete_function_for(params, context, is_server=True)


delete_function_tool = ToolDefinition(
    name="delete_function",
    description="Delete a UI function. DESTRUCTIVE — rejected by the backend if other entities still reference it.",
    parameters=[
        ToolParameter(name="name", type="string", description="Function name to delete"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ],
    execute=_execute_delete_function,
)


delete_server_function_tool = ToolDefinition(
    name="delete_server_function",
    description="Delete a server (core) function. DESTRUCTIVE.",
    parameters=[
        ToolParameter(name="name", type="string", description="Function name to delete"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ],
    execute=_execute_delete_server_function,
)


# ── DSL tools ────────────────────────────────────────────────────────────


async def _execute_compile_kirun_text(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    text = params.get("text") or ""
    if not text:
        return ToolResult(success=False, error="`text` is required")
    try:
        defn = kirun_dsl.compile_text(text)
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"Compile error: {type(e).__name__}: {e}")
    kirun_layout.auto_layout_definition(defn)
    return ToolResult(success=True, summary=json.dumps(defn, indent=2, default=str))


compile_kirun_text_tool = ToolDefinition(
    name="compile_kirun_text",
    description="Compile Kirun DSL text to a FunctionDefinition JSON (with positions auto-laid out). Doesn't save — preview only.",
    parameters=[
        ToolParameter(name="text", type="string", description="DSL source ('FUNCTION X NAMESPACE Y ...')"),
    ],
    execute=_execute_compile_kirun_text,
)


async def _execute_validate_kirun_text(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    text = params.get("text") or ""
    if not text:
        return ToolResult(success=False, error="`text` is required")
    ok, err = kirun_dsl.validate_text(text)
    return ToolResult(success=True, summary="OK" if ok else f"Invalid: {err}")


validate_kirun_text_tool = ToolDefinition(
    name="validate_kirun_text",
    description="Syntax-check Kirun DSL without saving. Returns 'OK' or parser error with line/column.",
    parameters=[
        ToolParameter(name="text", type="string", description="DSL source"),
    ],
    execute=_execute_validate_kirun_text,
)


async def _execute_format_kirun_text(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    text = params.get("text") or ""
    if not text:
        return ToolResult(success=False, error="`text` is required")
    try:
        formatted = await kirun_dsl.format_text(text)
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"Format error: {type(e).__name__}: {e}")
    return ToolResult(success=True, summary=formatted)


format_kirun_text_tool = ToolDefinition(
    name="format_kirun_text",
    description="Pretty-print Kirun DSL via compile-decompile round trip.",
    parameters=[
        ToolParameter(name="text", type="string", description="DSL source to format"),
    ],
    execute=_execute_format_kirun_text,
)


async def _execute_decompile_function_for(
    params: dict[str, Any], context: dict[str, Any], is_server: bool,
) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    existing, err = await _fetch_function_by_name(client, _fn_api(is_server), headers, ac, name)
    if err:
        return ToolResult(success=False, error=err)
    defn = (existing or {}).get("definition") or {}
    try:
        text = await kirun_dsl.decompile_json(defn)
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"Decompile error: {type(e).__name__}: {e}")
    return ToolResult(success=True, summary=text)


async def _execute_decompile_function(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    return await _execute_decompile_function_for(params, context, is_server=False)


decompile_function_tool = ToolDefinition(
    name="decompile_function",
    description="Fetch a UI Kirun function and return its DSL text. Use to read existing functions as text, edit, and save back via save_function_from_text.",
    parameters=[
        ToolParameter(name="name", type="string", description="Function name (full Namespace.LocalName)"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="is_server", type="boolean", required=False, default=False, description="True → decompile a server (core) function instead"),
    ],
    execute=_execute_decompile_function,
)


async def _execute_save_function_from_text(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    text = params.get("text") or ""
    is_server = bool(params.get("is_server", False))
    if not text:
        return ToolResult(success=False, error="`text` is required")
    try:
        defn = kirun_dsl.compile_text(text)
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"Compile error: {type(e).__name__}: {e}")
    kirun_layout.auto_layout_definition(defn)
    ns = defn.get("namespace")
    nm = defn.get("name")
    if not ns or not nm:
        return ToolResult(success=False, error="DSL must declare FUNCTION <name> NAMESPACE <namespace>")
    if ns == "UIEngine" or ns.startswith("System"):
        return ToolResult(success=False, error=f"Namespace '{ns}' is reserved.")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    cc = _resolve_client_code(params, context)
    full_name = f"{ns}.{nm}"
    api = _fn_api(is_server)
    client, headers = _client_and_headers(context)

    # Upsert: if exists, update; else create.
    listing = await client.get(api, headers=headers, params={"page": 0, "size": 1, "appCode": ac, "name": full_name})
    if not listing.success:
        return ToolResult(success=False, error=listing.error)
    content = (listing.data or {}).get("content", []) if isinstance(listing.data, dict) else []
    message = params.get("message") or "Saved from DSL via CFA"
    if content:
        fn_id = content[0].get("id")
        detail = await client.get(f"{api}/{fn_id}", headers=headers)
        if not detail.success:
            return ToolResult(success=False, error=detail.error)
        existing = detail.data if isinstance(detail.data, dict) else {}
        existing["definition"] = defn
        existing["message"] = message
        r = await client.put(f"{api}/{fn_id}", headers=headers, json=existing)
        verb = "Updated"
    else:
        body = {"name": full_name, "appCode": ac, "clientCode": cc, "message": message, "definition": defn}
        r = await client.post(api, headers=headers, json=body)
        verb = "Created"
    if not r.success:
        return ToolResult(success=False, error=r.error)
    fid = (r.data or {}).get("id", "?") if isinstance(r.data, dict) else "?"
    return ToolResult(success=True, summary=f"{verb} {_fn_kind(is_server)} '{full_name}' (id={fid}).")


save_function_from_text_tool = ToolDefinition(
    name="save_function_from_text",
    description="Compile Kirun DSL and create/update a function. Name + namespace come from the DSL. Auto-layout runs before save so the visual editor opens onto a readable DAG. Use is_server=True for the core runtime.",
    parameters=[
        ToolParameter(name="text", type="string", description="Full DSL source"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description="Owning clientCode"),
        ToolParameter(name="is_server", type="boolean", required=False, default=False, description="Save to server (core) runtime"),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG),
    ],
    execute=_execute_save_function_from_text,
)


# ── Kirun primitives ─────────────────────────────────────────────────────


def _builtin_catalog(runtime: str) -> set[str]:
    """Conventions-defined built-ins for the requested runtime."""
    out: set[str] = set()
    for ns, names in c.KIRUN_NAMESPACES.items():
        for n in names:
            out.add(f"{ns}.{n}")
    if runtime == "ui":
        for n in c.UIENGINE_PRIMITIVES:
            out.add(f"UIEngine.{n}")
    return out


async def _execute_list_kirun_primitives(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    runtime = (params.get("runtime") or "ui").strip().lower()
    if runtime not in ("ui", "core"):
        return ToolResult(success=False, error="`runtime` must be 'ui' or 'core'")
    filter_re_str = params.get("filter") or ".*"
    include_builtins = bool(params.get("include_builtins", True))

    ac, _ = _resolve_app_code(params, context)
    cc = _resolve_client_code(params, context)
    client, headers = _client_and_headers(context)

    request_params: dict[str, Any] = {
        "filter": filter_re_str,
        "includeKIRunRepos": "true" if include_builtins else "false",
    }
    if ac:
        request_params["appCode"] = ac
    if cc:
        request_params["clientCode"] = cc

    platform_names: set[str] = set()
    platform_error: str | None = None
    r = await client.get(f"/api/{runtime}/functions/repositoryFilter", headers=headers, params=request_params)
    if r.success:
        platform_names = set(r.data) if isinstance(r.data, list) else set()
    else:
        platform_error = r.error

    builtin_names: set[str] = _builtin_catalog(runtime) if include_builtins else set()

    try:
        pattern = re.compile(filter_re_str)
    except re.error as e:
        return ToolResult(success=False, error=f"Invalid filter regex {filter_re_str!r}: {e}")

    seen: set[str] = set()
    entries: list[tuple[str, str]] = []
    for n in sorted(platform_names):
        if not pattern.search(n) or n in seen:
            continue
        seen.add(n)
        entries.append((n, "platform"))
    for n in sorted(builtin_names):
        if not pattern.search(n) or n in seen:
            continue
        seen.add(n)
        entries.append((n, "builtin"))

    counts = {
        "platform": sum(1 for _, s in entries if s == "platform"),
        "builtin": sum(1 for _, s in entries if s == "builtin"),
    }
    name_width = max((len(n) for n, _ in entries), default=0)
    body_lines = [f"{n.ljust(name_width)}  ({s})" for n, s in entries]
    header = (
        f"{runtime} primitives · filter='{filter_re_str}' · "
        f"include_builtins={include_builtins} · {len(entries)} matches"
        + (f" · platform={counts['platform']} builtin={counts['builtin']}" if entries else "")
    )
    note = f"\n(note: platform endpoint returned error: {platform_error})\n" if platform_error else ""
    return ToolResult(success=True, summary=f"{header}:{note}\n" + "\n".join(body_lines))


list_kirun_primitives_tool = ToolDefinition(
    name="list_kirun_primitives",
    description="List Kirun primitives + app-defined functions matching a regex. Merges platform discovery (live) with conventions-defined built-ins. Sources: platform | builtin.",
    parameters=[
        ToolParameter(name="filter", type="string", required=False, default=".*", description="Regex over fully-qualified names"),
        ToolParameter(name="runtime", type="string", required=False, default="ui", description="ui | core"),
        ToolParameter(name="include_builtins", type="boolean", required=False, default=True, description="Layer Kirun built-ins under platform functions"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description=_DESC_CLIENT_CODE),
    ],
    execute=_execute_list_kirun_primitives,
)


async def _execute_get_kirun_primitive(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    namespace = (params.get("namespace") or "").strip()
    name = (params.get("name") or "").strip()
    if not namespace or not name:
        return ToolResult(success=False, error="`namespace` and `name` are required")
    runtime = (params.get("runtime") or "ui").strip().lower()
    if runtime not in ("ui", "core"):
        return ToolResult(success=False, error="`runtime` must be 'ui' or 'core'")
    include_builtins = bool(params.get("include_builtins", True))
    ac, _ = _resolve_app_code(params, context)
    cc = _resolve_client_code(params, context)
    client, headers = _client_and_headers(context)
    request_params: dict[str, Any] = {
        "namespace": namespace, "name": name,
        "includeKIRunRepos": "true" if include_builtins else "false",
    }
    if ac:
        request_params["appCode"] = ac
    if cc:
        request_params["clientCode"] = cc
    r = await client.get(f"/api/{runtime}/functions/repositoryFind", headers=headers, params=request_params)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"{namespace}.{name} ({runtime}):\n{json.dumps(r.data, indent=2, default=str)}")


get_kirun_primitive_tool = ToolDefinition(
    name="get_kirun_primitive",
    description="Fetch a Kirun primitive's full signature (parameters, events, schema) from the live platform.",
    parameters=[
        ToolParameter(name="namespace", type="string", description="e.g. 'System.Math', 'UIEngine', 'MyApp'"),
        ToolParameter(name="name", type="string", description="Primitive name (e.g. 'Add', 'SetStore')"),
        ToolParameter(name="runtime", type="string", required=False, default="ui", description="ui | core"),
        ToolParameter(name="include_builtins", type="boolean", required=False, default=True, description="Include Kirun built-ins"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description=_DESC_CLIENT_CODE),
    ],
    execute=_execute_get_kirun_primitive,
)


# ── execute_function ─────────────────────────────────────────────────────


async def _execute_function_invocation(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    namespace = (params.get("namespace") or "").strip()
    name = (params.get("name") or "").strip()
    if not namespace or not name:
        return ToolResult(success=False, error="`namespace` and `name` are required")
    runtime = (params.get("runtime") or "core").strip().lower()
    if runtime not in ("ui", "core"):
        return ToolResult(success=False, error="`runtime` must be 'ui' or 'core'")
    arguments = params.get("arguments") or {}
    mocks = params.get("mocks")
    sim_context = params.get("context")
    dry_run = bool(params.get("dry_run", False))
    include_trace = bool(params.get("include_trace", False))

    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    path = f"/api/{runtime}/function/execute/{namespace}/{name}"

    if mocks or sim_context or dry_run or include_trace:
        body: Any = {"arguments": arguments}
        if mocks:
            body["mocks"] = mocks
        if sim_context:
            body["context"] = sim_context
        if dry_run:
            body["dryRun"] = True
        if include_trace:
            body["includeTrace"] = True
    else:
        body = arguments

    r = await client.post(path, headers=headers, json=body)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    body_text = r.data if isinstance(r.data, str) else json.dumps(r.data, indent=2, default=str)
    flags: list[str] = []
    if mocks:
        flags.append(f"mocks={len(mocks)}")
    if sim_context:
        flags.append(f"context={len(sim_context)}")
    if dry_run:
        flags.append("dry_run=True")
    if include_trace:
        flags.append("include_trace=True")
    flag_blob = f" [{', '.join(flags)}]" if flags else ""
    return ToolResult(success=True, summary=f"Executed {namespace}.{name} on {runtime} ({ac}){flag_blob}:\n{body_text}")


execute_function_tool = ToolDefinition(
    name="execute_function",
    description="Invoke a function with arguments and return its output. Optional: mocks (substitute dependency outputs), context (preseed Page/Store/Context paths), dry_run (suppress side effects), include_trace (return per-step DebugCollector log). SIDE-EFFECTING unless dry_run=true.",
    parameters=[
        ToolParameter(name="namespace", type="string", description="Function namespace"),
        ToolParameter(name="name", type="string", description="Function local name"),
        ToolParameter(name="arguments", type="object", required=False, description="{paramName: value}"),
        ToolParameter(name="runtime", type="string", required=False, default="core", description="ui | core"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description=_DESC_CLIENT_CODE),
        ToolParameter(name="mocks", type="object", required=False, description="Stub outputs by fully-qualified callable"),
        ToolParameter(name="context", type="object", required=False, description="Preseed Page/Store/Context paths"),
        ToolParameter(name="dry_run", type="boolean", required=False, default=False, description="Suppress side-effect steps"),
        ToolParameter(name="include_trace", type="boolean", required=False, default=False, description="Return per-step trace"),
    ],
    execute=_execute_function_invocation,
)


# ── Function step ops (PATCH /api/ui/functions/{id}/steps) ──────────────


def _build_step_statement(
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


async def _patch_function_steps(
    fn: dict[str, Any], steps_to_merge: dict[str, Any],
    message: str, is_server: bool, context: dict[str, Any],
) -> tuple[bool, str]:
    """Patch steps on a function. UI uses surgical PATCH; server uses full-doc PUT."""
    client, headers = _client_and_headers(context)
    fn_id = fn.get("id")
    if not fn_id:
        return False, "function has no id"
    if is_server:
        merged = dict(fn)
        defn = dict(merged.get("definition") or {})
        steps = dict(defn.get("steps") or {})
        steps.update(steps_to_merge)
        defn["steps"] = steps
        merged["definition"] = defn
        merged["message"] = message
        r = await client.put(f"{_CORE_FN_API}/{fn_id}", headers=headers, json=merged)
    else:
        body = {
            "steps": steps_to_merge,
            "expectedVersion": c.expected_version_for(fn),
            "message": message,
        }
        r = await client.patch(f"{_UI_FN_API}/{fn_id}/steps", headers=headers, json=body)
    if not r.success:
        return False, r.error
    return True, ""


async def _execute_add_step(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    function_name = (params.get("function_name") or "").strip()
    step_name = (params.get("step_name") or "").strip()
    primitive_namespace = (params.get("primitive_namespace") or "").strip()
    primitive_name = (params.get("primitive_name") or "").strip()
    if not all([function_name, step_name, primitive_namespace, primitive_name]):
        return ToolResult(success=False, error="`function_name`, `step_name`, `primitive_namespace`, `primitive_name` are required")
    name_err = c.validate_simple_name(step_name)
    if name_err:
        return ToolResult(success=False, error=f"step_name — {name_err}")
    is_server = bool(params.get("is_server", False))
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    fn, err = await _fetch_function_by_name(client, _fn_api(is_server), headers, ac, function_name)
    if err:
        return ToolResult(success=False, error=err)
    assert fn is not None
    existing_steps = (fn.get("definition") or {}).get("steps") or {}
    if step_name in existing_steps:
        return ToolResult(success=False, error=f"Step '{step_name}' already exists in '{function_name}'. Use update_step.")

    warning = c.validate_step_call(primitive_namespace, primitive_name)
    statement = _build_step_statement(
        step_name=step_name,
        primitive_namespace=primitive_namespace,
        primitive_name=primitive_name,
        params=params.get("params"),
        dependencies=params.get("dependencies"),
        position_left=float(params.get("position_left") or 0),
        position_top=float(params.get("position_top") or 0),
    )
    ok, err = await _patch_function_steps(
        fn, {step_name: statement},
        params.get("message") or "Added step via CFA", is_server, context,
    )
    if not ok:
        return ToolResult(success=False, error=err)
    msg = f"Added step '{step_name}' ({primitive_namespace}.{primitive_name}) to {_fn_kind(is_server)} '{function_name}'."
    if warning:
        msg += f"\nWarning: {warning}"
    return ToolResult(success=True, summary=msg)


add_step_tool = ToolDefinition(
    name="add_step",
    description="Add a step to a function via surgical PATCH (UI) or full PUT (server). Active dependencies use the same format as set_dependencies. Wires position auto-set to (0,0); use auto-layout via save_function_from_text for a clean visual.",
    parameters=[
        ToolParameter(name="function_name", type="string", description="Full function name (Namespace.LocalName)"),
        ToolParameter(name="step_name", type="string", description="Unique step name within the function"),
        ToolParameter(name="primitive_namespace", type="string", description="e.g. 'System.Math', 'UIEngine', 'MyApp'"),
        ToolParameter(name="primitive_name", type="string", description="Primitive name (e.g. 'Add', 'SetStore')"),
        ToolParameter(name="params", type="object", required=False, description="Parameter values (auto-coerce: Steps./Page./etc. → expressions)"),
        ToolParameter(name="dependencies", type="array", required=False, description="'stepName' or 'Steps.<step>.<event>'", items={"type": "string"}),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="is_server", type="boolean", required=False, default=False, description="Target server (core) function"),
        ToolParameter(name="position_left", type="number", required=False, default=0, description="Editor x"),
        ToolParameter(name="position_top", type="number", required=False, default=0, description="Editor y"),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG),
    ],
    execute=_execute_add_step,
)


async def _execute_update_step(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    function_name = (params.get("function_name") or "").strip()
    step_name = (params.get("step_name") or "").strip()
    if not function_name or not step_name:
        return ToolResult(success=False, error="`function_name` and `step_name` are required")
    is_server = bool(params.get("is_server", False))
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    fn, err = await _fetch_function_by_name(client, _fn_api(is_server), headers, ac, function_name)
    if err:
        return ToolResult(success=False, error=err)
    assert fn is not None
    steps = (fn.get("definition") or {}).get("steps") or {}
    if step_name not in steps:
        return ToolResult(success=False, error=f"Step '{step_name}' not found in '{function_name}'.")

    statement = dict(steps[step_name])
    changed: list[str] = []
    new_ns = params.get("primitive_namespace")
    new_name = params.get("primitive_name")
    new_params = params.get("params")
    pos_left = params.get("position_left")
    pos_top = params.get("position_top")
    if new_ns is not None:
        statement["namespace"] = new_ns
        changed.append("namespace")
    if new_name is not None:
        statement["name"] = new_name
        changed.append("primitive")
    if new_params is not None:
        statement["parameterMap"] = c.make_parameter_map(new_params)
        changed.append("parameterMap")
    if pos_left is not None or pos_top is not None:
        pos = dict(statement.get("position") or {})
        if pos_left is not None:
            pos["left"] = float(pos_left)
        if pos_top is not None:
            pos["top"] = float(pos_top)
        statement["position"] = pos
        changed.append("position")
    if not changed:
        return ToolResult(success=True, summary="No-op: nothing to update.")

    ok, err = await _patch_function_steps(
        fn, {step_name: statement},
        params.get("message") or "Updated step via CFA", is_server, context,
    )
    if not ok:
        return ToolResult(success=False, error=err)
    return ToolResult(success=True, summary=f"Updated step '{step_name}' in {_fn_kind(is_server)} '{function_name}': {', '.join(changed)}.")


update_step_tool = ToolDefinition(
    name="update_step",
    description="Replace fields on an existing step via surgical PATCH. params REPLACES the full parameterMap.",
    parameters=[
        ToolParameter(name="function_name", type="string", description=_DESC_FN_NAME_SHORT),
        ToolParameter(name="step_name", type="string", description="Step to update"),
        ToolParameter(name="params", type="object", required=False, description="REPLACES the full parameterMap"),
        ToolParameter(name="primitive_namespace", type="string", required=False, description="New namespace"),
        ToolParameter(name="primitive_name", type="string", required=False, description="New primitive"),
        ToolParameter(name="position_left", type="number", required=False, description="Update editor x"),
        ToolParameter(name="position_top", type="number", required=False, description="Update editor y"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="is_server", type="boolean", required=False, default=False, description=_DESC_TARGET_SERVER),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG),
    ],
    execute=_execute_update_step,
)


async def _execute_set_dependencies(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    function_name = (params.get("function_name") or "").strip()
    step_name = (params.get("step_name") or "").strip()
    depends_on = params.get("depends_on")
    if not function_name or not step_name or not isinstance(depends_on, list):
        return ToolResult(success=False, error="`function_name`, `step_name`, `depends_on` (list) are required")
    is_server = bool(params.get("is_server", False))
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    fn, err = await _fetch_function_by_name(client, _fn_api(is_server), headers, ac, function_name)
    if err:
        return ToolResult(success=False, error=err)
    assert fn is not None
    steps = (fn.get("definition") or {}).get("steps") or {}
    if step_name not in steps:
        return ToolResult(success=False, error=f"Step '{step_name}' not found in '{function_name}'.")

    dep_map: dict[str, bool] = {}
    for d in depends_on:
        if d.startswith("Steps."):
            dep_map[d] = True
        else:
            dep_map[c.make_dependency_key(d, "output")] = True
    statement = dict(steps[step_name])
    statement["dependentStatements"] = dep_map
    ok, err = await _patch_function_steps(
        fn, {step_name: statement},
        params.get("message") or "Updated dependencies via CFA", is_server, context,
    )
    if not ok:
        return ToolResult(success=False, error=err)
    return ToolResult(success=True, summary=f"Set dependencies on '{step_name}' in {_fn_kind(is_server)} '{function_name}': {list(dep_map.keys()) or '(none)'}.")


set_dependencies_tool = ToolDefinition(
    name="set_dependencies",
    description="Replace a step's dependentStatements (active scheduling edges). Implicit deps from expressions always apply regardless. Pass [] to clear.",
    parameters=[
        ToolParameter(name="function_name", type="string", description=_DESC_FN_NAME_SHORT),
        ToolParameter(name="step_name", type="string", description="Step whose dependencies to set"),
        ToolParameter(name="depends_on", type="array", description="'stepName' or 'Steps.<step>.<event>' entries", items={"type": "string"}),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="is_server", type="boolean", required=False, default=False, description=_DESC_TARGET_SERVER),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG),
    ],
    execute=_execute_set_dependencies,
)


def _find_dangling_refs(steps: dict[str, Any], removed: str) -> list[str]:
    """Other steps that explicitly or implicitly reference `removed` — for warnings."""
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


async def _execute_remove_step(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    function_name = (params.get("function_name") or "").strip()
    step_name = (params.get("step_name") or "").strip()
    if not function_name or not step_name:
        return ToolResult(success=False, error="`function_name` and `step_name` are required")
    is_server = bool(params.get("is_server", False))
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    fn, err = await _fetch_function_by_name(client, _fn_api(is_server), headers, ac, function_name)
    if err:
        return ToolResult(success=False, error=err)
    assert fn is not None
    defn = fn.get("definition") or {}
    steps = dict(defn.get("steps") or {})
    if step_name not in steps:
        return ToolResult(success=False, error=f"Step '{step_name}' not found in '{function_name}'.")
    dangling = _find_dangling_refs(steps, step_name)
    steps.pop(step_name)
    defn["steps"] = steps
    fn["definition"] = defn
    fn["message"] = params.get("message") or "Removed step via CFA"
    r = await client.put(f"{_fn_api(is_server)}/{fn.get('id')}", headers=headers, json=fn)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    msg = f"Removed step '{step_name}' from {_fn_kind(is_server)} '{function_name}'."
    if dangling:
        msg += "\nWARNING: dangling refs:\n  - " + "\n  - ".join(dangling)
    return ToolResult(success=True, summary=msg)


remove_step_tool = ToolDefinition(
    name="remove_step",
    description="Remove a step from a function (full-doc PUT — no surgical DELETE endpoint exists). Warns about other steps that still reference it.",
    parameters=[
        ToolParameter(name="function_name", type="string", description=_DESC_FN_NAME_SHORT),
        ToolParameter(name="step_name", type="string", description="Step to remove"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="is_server", type="boolean", required=False, default=False, description=_DESC_TARGET_SERVER),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG),
    ],
    execute=_execute_remove_step,
)


# ── Module export ────────────────────────────────────────────────────────


TOOLS: list[ToolDefinition] = [
    # Function CRUD
    list_functions_tool,
    list_server_functions_tool,
    get_function_tool,
    get_server_function_tool,
    create_function_tool,
    create_server_function_tool,
    update_function_tool,
    update_server_function_tool,
    delete_function_tool,
    delete_server_function_tool,
    # DSL
    compile_kirun_text_tool,
    validate_kirun_text_tool,
    format_kirun_text_tool,
    decompile_function_tool,
    save_function_from_text_tool,
    # Primitives
    list_kirun_primitives_tool,
    get_kirun_primitive_tool,
    # Execution
    execute_function_tool,
    # Function step ops (PATCH /steps for UI, full PUT for server)
    add_step_tool,
    update_step_tool,
    set_dependencies_tool,
    remove_step_tool,
]
