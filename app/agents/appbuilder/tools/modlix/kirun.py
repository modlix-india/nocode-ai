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

# Cap for decompiled DSL, well above the 4K default (see the ToolResult field).
# Measured over the 526 functions on the platform: p50 1.5K, p90 7.1K, p95 12.9K.
# 4K truncated 19% of them; 32K delivers all but 8 in a single call, and those 8
# are paged via the `part` parameter rather than truncated.
_DECOMPILE_MAX_CHARS = 32000
# Headroom so the part header/footer can never push a full part into truncation.
_PART_RESULT_CHARS = _DECOMPILE_MAX_CHARS + 1000

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


def _namespace_of(full_name: Any) -> str:
    """Namespace half of a "<namespace>.<localName>" function name.

    Namespaces themselves contain dots (Authzump.sso.Login is local name `Login`
    in namespace `Authzump.sso`), so this splits on the LAST dot.
    """
    if not isinstance(full_name, str) or "." not in full_name:
        return ""
    return full_name.rsplit(".", 1)[0]


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
    namespace = (params.get("namespace") or "").strip()
    client, headers = _client_and_headers(context)
    api = _fn_api(is_server)
    # `namespace` is NOT sent to the API: no function document carries a top-level
    # `namespace` field (it lives inside `definition`), so the server-side filter
    # matched nothing and the tool silently reported an empty namespace. Filter
    # here instead, off the name, which is always "<namespace>.<localName>" and
    # was verified equal to definition.namespace for every function on the platform.
    # Fetch wide when filtering so the match is not limited to the first page.
    fetch_size = 1000 if namespace else size
    request_params: dict[str, Any] = {"page": 0, "size": fetch_size, "appCode": ac}
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
    if namespace:
        rows = [x for x in rows if _namespace_of(x.get("name")) == namespace][:size]
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


def _parameter_names(parameters: Any) -> list[str]:
    """`parameters` is a Map<String, Parameter> keyed by name in the KIRun model.
    A few older definitions carry a list of Parameter objects instead."""
    if isinstance(parameters, dict):
        return [
            (v.get("parameterName") if isinstance(v, dict) else None) or k
            for k, v in parameters.items()
        ]
    if isinstance(parameters, list):
        return [
            p.get("parameterName") if isinstance(p, dict) else str(p)
            for p in parameters
        ]
    return []


def _function_summary(fn: dict[str, Any]) -> dict[str, Any]:
    defn = fn.get("definition")
    if not isinstance(defn, dict):
        defn = {}
    events = defn.get("events")
    steps = defn.get("steps")
    return {
        "id": fn.get("id"),
        "name": fn.get("name"),
        "namespace": defn.get("namespace") or fn.get("namespace"),
        "version": fn.get("version"),
        "clientCode": fn.get("clientCode"),
        "stepCount": len(steps) if isinstance(steps, (dict, list)) else 0,
        "events": list(events.keys()) if isinstance(events, dict) else [],
        "parameters": _parameter_names(defn.get("parameters")),
    }


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
        # Same paging as decompile_function: a 4K slice of a function's JSON is
        # both unreadable and silently partial. Prefer decompile_function for
        # reading logic; this stays for when the raw JSON is genuinely needed.
        tool = "get_server_function" if is_server else "get_function"
        return _paged_result(
            json.dumps(fn, indent=2, default=str), name, params,
            f'{tool}(name="{name}", include="full", part=%d)',
        )
    return ToolResult(
        success=True, summary=json.dumps(_function_summary(fn), indent=2, default=str),
    )


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
        ToolParameter(name="part", type="integer", required=False, default=1, description="Which part of a large full read to return. Only needed when a previous result said 'part N of M'."),
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
        ToolParameter(name="part", type="integer", required=False, default=1, description="Which part of a large full read to return. Only needed when a previous result said 'part N of M'."),
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
        "namespace": namespace, "name": name, "steps": {}, "events": {}, "parameters": {},
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


_COMPILE_HINT_RULES: list[tuple[re.Pattern[str], str]] = [
    # Must stay FIRST: the generic "expected ..." rule below also matches
    # "Expected: RIGHT_PAREN" and would send the model looking for a missing
    # GenerateEvent step instead of the real cause. In the Chit Fund run this
    # error cost two turns and then pushed the model into hardcoding ids
    # rather than fixing the expression.
    (
        # `and` / `or` are tokenised as IDENTIFIER, so a boolean expression in
        # argument position fails with "Actual: IDENTIFIER (and)".
        re.compile(r"Expected:?\s*(?:RIGHT_PAREN|RIGHT_BRACE|RIGHT_BRACKET|COMMA)[\s\S]*?Actual:?\s*(?:OPERATOR|EQUALS|IDENTIFIER\s*\((?:and|or)\))", re.IGNORECASE),
        "Next step: an argument value that STARTS with a double-quoted string, true/false or null and then continues with an operator (`\"Store.x.\" + Parent.id`) is read as a plain literal, so the operator breaks the parse. Wrap the WHOLE expression in parentheses: `path = (\"Store.paidIds.\" + Parent.memberId)`. Fix only that expression; do not restructure the step or hardcode values to avoid it.",
    ),
    (
        re.compile(r"(?:unknown|not\s+found|undefined)\s+(?:primitive|function|namespace)\W*(\w[\w.]*)", re.IGNORECASE),
        "Next step: call `get_kirun_primitive(namespace=\"<ns>\", name=\"<n>\")` on the named primitive to check the exact spelling + signature. The primitive is likely capitalised differently (e.g. `System.Math.Add`, not `system.math.add`) or lives in a different namespace.",
    ),
    (
        re.compile(r"(?:missing|expected|required).*?:\s*(\w+)", re.IGNORECASE),
        "Next step: the parser names the missing piece. Re-read the DSL shape in `compile_kirun_text`'s description — common gaps are the final `System.GenerateEvent` step, an EVENTS block, or a closing `}`.",
    ),
    (
        re.compile(r"(?:unexpected|invalid)\s+token", re.IGNORECASE),
        "Next step: a syntax token broke parsing. Check that schema literals use array-typed `type` (e.g. `{\"type\":[\"INTEGER\"]}`, NOT `{\"type\":\"INTEGER\"}`) and that JSON values inside parentheses are valid JSON.",
    ),
    (
        re.compile(r"(?:type|schema).*?(?:invalid|mismatch|wrong)", re.IGNORECASE),
        "Next step: a schema is malformed. ALL `type` fields MUST be arrays — `{\"type\":[\"INTEGER\"]}` is correct, `{\"type\":\"INTEGER\"}` is not.",
    ),
    (
        re.compile(r"(?:step|dependency|after).*?(?:unknown|missing|invalid)", re.IGNORECASE),
        "Next step: an `AFTER Steps.<name>.<event>` references a step or event that doesn't exist. Confirm the step name and the event name (`output`/`error`/`true`/`false`/`iteration`) match the upstream step's declared events.",
    ),
    (
        re.compile(r"line\s+(\d+)(?:.*?col(?:umn)?\s+(\d+))?", re.IGNORECASE),
        "Next step: the parser points at a specific line/column. Read that line in your DSL and check indentation (steps under `LOGIC` use spaces, nested `output` blocks indent further).",
    ),
]


def _hint_for_compile_error(error_text: str) -> str:
    """Pick the FIRST matching hint rule for a compile/validate error.

    The error message itself carries the precise failure; we add one
    `Next step:` line so the LLM doesn't have to guess which research tool
    to reach for. Multiple matches → only the first hint surfaces; we'd
    rather give one clear instruction than three competing ones.
    """
    for pattern, hint in _COMPILE_HINT_RULES:
        if pattern.search(error_text):
            return hint
    return (
        "Next step: re-read the DSL shape in `compile_kirun_text`'s description. If the "
        "error names a primitive, call `get_kirun_primitive` to confirm its signature."
    )


async def _execute_compile_kirun_text(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    text = params.get("text") or ""
    if not text:
        return ToolResult(success=False, error="`text` is required")
    try:
        defn = kirun_dsl.compile_text(text)
    except Exception as e:  # noqa: BLE001
        raw = f"{type(e).__name__}: {e}"
        return ToolResult(success=False, error=f"Compile error: {raw}\n\n{_hint_for_compile_error(raw)}")
    kirun_layout.auto_layout_definition(defn)
    return ToolResult(success=True, summary=json.dumps(defn, indent=2, default=str))


compile_kirun_text_tool = ToolDefinition(
    name="compile_kirun_text",
    description="""Compile Kirun DSL text to a FunctionDefinition JSON without saving. Returns the compiled JSON on success, or a parser/validation error with line+column on failure.

IMPORTANT: Use this BEFORE `save_function_from_text` to catch DSL syntax errors fast (no network round-trip to the gateway). Once it compiles cleanly, save with `save_function_from_text`.

When authoring a Kirun function:
1. WRITE FIRST. The DSL shape below covers 80% of real functions — start from it. Don't pre-research unless the FIRST compile fails.
2. If compile fails: the error tells you the specific issue. Look up only the named primitive (`get_kirun_primitive`) — don't re-read the whole catalog.
3. Don't compile 10 incremental drafts. One write, one compile, fix from the error, save. DeepSeek's reasoning mode in particular tends to over-iterate — resist that here.

DSL shape (minimum viable function):
```
FUNCTION AddNumbers
    NAMESPACE MyApp
    PARAMETERS
        a AS {"type":["INTEGER"]}
        b AS {"type":["INTEGER"]}
    EVENTS
        output
            result AS {"type":["INTEGER"]}
    LOGIC
        add: System.Math.Add(undefined = Arguments.a, undefined = Arguments.b)
            output
                event: System.GenerateEvent(eventName = "output", results = {"name": "result", "value": {"isExpression": true, "value": "Steps.add.output.value"}})
```

Key production rules:
- Each step is `<stepName>: <Namespace>.<PrimitiveName>(<param> = <value>, ...)` — one primitive per step.
- Step dependencies are EITHER nested under the parent step's `output` block (preferred) OR explicit with `AFTER Steps.<step>.<event>` (for cross-branch wiring).
- Pass `Arguments.<paramName>` to reference function inputs, `Steps.<stepName>.output.value` to reference prior step results, and `Context.<key>` for context variables.
- Final `System.GenerateEvent` produces the function's output event — use `{"isExpression": true, "value": "..."}` inside `results` to reference upstream step outputs.
- Schemas are inline JSON; `type` is ALWAYS an array (`["INTEGER"]` not `"INTEGER"`).""",
    parameters=[
        ToolParameter(name="text", type="string", description="DSL source starting with `FUNCTION <name>` then `NAMESPACE`, `PARAMETERS`, `EVENTS`, `LOGIC`"),
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


def _split_dsl(text: str, size: int) -> list[str]:
    """Split DSL text into <=size chunks on line boundaries.

    A single line longer than `size` is emitted whole rather than cut: an over-long
    line is one giant inline schema, and halving it produces invalid DSL either side.
    """
    parts: list[str] = []
    current: list[str] = []
    used = 0
    for line in text.splitlines(keepends=True):
        if current and used + len(line) > size:
            parts.append("".join(current))
            current, used = [], 0
        current.append(line)
        used += len(line)
    if current:
        parts.append("".join(current))
    return parts or [text]


def _clamp_part(raw: Any, total: int) -> int:
    try:
        part = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, min(part, total))


def _paged_result(text: str, label: str, params: dict[str, Any], next_call: str) -> ToolResult:
    """Return `text` whole, or as one numbered part with the call for the next.

    Paging rather than truncating, because a truncated read of a function looks
    complete: there is no other tool that can reach the tail, so the model would
    answer from the fragment. `next_call` is a printf-style template taking the
    next part number.
    """
    parts = _split_dsl(text, _DECOMPILE_MAX_CHARS)
    if len(parts) == 1:
        return ToolResult(success=True, summary=text, max_result_chars=_PART_RESULT_CHARS)

    part = _clamp_part(params.get("part"), len(parts))
    body = parts[part - 1]
    head = (
        f"[{label}: part {part} of {len(parts)}. Whole content is {len(text):,} chars; "
        f"this part is {len(body):,}. Lines are never split across parts.]\n\n"
    )
    if part < len(parts):
        tail = (
            f"\n\n[End of part {part}. This is NOT the whole thing. To continue, call "
            f"{next_call % (part + 1)}. Do not describe or edit it until you have read "
            f"all {len(parts)} parts.]"
        )
    else:
        tail = f"\n\n[End of part {part} of {len(parts)}. You have now read all of it.]"
    return ToolResult(success=True, summary=head + body + tail,
                      max_result_chars=_PART_RESULT_CHARS)


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

    # The point of a decompile is the WHOLE function: a DSL cut mid-step still
    # reads as complete, so the model explains logic it never saw. 32K delivers
    # 98.5% of the platform's functions in one call; the rest page.
    srv = ", is_server=true" if is_server else ""
    return _paged_result(
        text, name, params, f'decompile_function(name="{name}"{srv}, part=%d)',
    )


async def _execute_decompile_function(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    return await _execute_decompile_function_for(
        params, context, is_server=bool(params.get("is_server", False)),
    )


decompile_function_tool = ToolDefinition(
    name="decompile_function",
    description="""Fetch a Kirun function from the platform and return its DSL text. The canonical way to READ an existing function — the DSL is more compact and editable than the underlying JSON.

START HERE before authoring a new Kirun function. Read 1-2 similar existing functions first, then mimic their step structure, dependency pattern, and output-event shape. Authoring blind without reading prior art is the single most common cause of compile failures.

Typical flow:
1. `list_kirun_primitives(filter="MyApp\\.")` to discover similar functions in the same namespace.
2. `decompile_function(name="MyApp.SimilarFunction")` to read the working DSL.
3. Author your new function, then `compile_kirun_text` to validate before saving.
4. `save_function_from_text` to persist (auto-runs compile + layout).

To EDIT an existing function:
- `decompile_function` to get its text → modify the text → `save_function_from_text` to round-trip back. This replaces the whole function in one shot.
- For surgical step-level edits (add/remove/rewire one step), prefer `add_step`, `update_step`, `remove_step`, `set_dependencies` — those operate on the function's step map directly without rewriting other steps.

Pass `is_server=True` to decompile a server (core-runtime) function. Default `false` targets the UI runtime where most app-level functions live.

Nearly every function returns whole in one call. A very large one (>32K of DSL) comes back in numbered parts: the result says "part 1 of N" and ends by telling you the exact call for the next part. When that happens, read EVERY part before describing or editing the function — a partial read looks complete and will make you explain steps you never saw.""",
    parameters=[
        ToolParameter(name="name", type="string", description="Function name (full Namespace.LocalName, e.g. 'MyApp.AddNumbers')"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="is_server", type="boolean", required=False, default=False, description="True → decompile a server (core) function instead"),
        ToolParameter(name="part", type="integer", required=False, default=1, description="Which part to return, for a function too large to send at once. Only needed when a previous result said 'part N of M'."),
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
        raw = f"{type(e).__name__}: {e}"
        return ToolResult(success=False, error=f"Compile error: {raw}\n\n{_hint_for_compile_error(raw)}")
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
    description="""Compile Kirun DSL and create/update a function in one round-trip. Compiles, auto-lays out step positions, and either creates a new function or updates an existing one (name + namespace are taken from the DSL itself). Returns the saved function ID + name on success.

IMPORTANT: ALWAYS call `compile_kirun_text` FIRST with the same DSL text to catch parser errors without hitting the gateway. Save only after the DSL compiles cleanly — otherwise you pay a network round-trip just to learn it doesn't parse.

This is the right tool when:
- You authored a new function from scratch (after reading similar functions via `decompile_function`).
- You're replacing an existing function wholesale (decompile → edit text → save back).

NOT the right tool when:
- You only need to add/remove/edit ONE step — use `add_step` / `update_step` / `remove_step` instead (surgical, preserves other steps' layout).
- You're tweaking just dependencies — use `set_dependencies`.

If the platform rejects the save (validation error, schema mismatch, primitive doesn't exist), the error message includes the exact failure. Re-read the relevant primitive via `get_kirun_primitive` and retry.""",
    parameters=[
        ToolParameter(name="text", type="string", description="Full DSL source starting with `FUNCTION <name>`. Must compile cleanly via `compile_kirun_text` first."),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description="Owning clientCode"),
        ToolParameter(name="is_server", type="boolean", required=False, default=False, description="Save to server (core) runtime instead of UI"),
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


def _uiengine_primitive_result(name: str) -> ToolResult:
    """Answer UIEngine.* lookups from the generated catalog.

    The platform's /functions/repositoryFind does not know browser-side
    builtins and returned a literal `null` for them (with success=True), so
    the model could neither confirm a real function nor learn that a guessed
    one does not exist.
    """
    sig = c.UIENGINE_SIGNATURES.get(name)
    if sig is None:
        known = ", ".join(sorted(c.UIENGINE_SIGNATURES))
        return ToolResult(
            success=False,
            error=(
                f"UIEngine.{name} does not exist. Browser-side UIEngine functions are: {known}. "
                "For storage rows use FetchData (GET), SendData (POST/PUT) and DeleteData; "
                "there is no UIEngine.Read/Create/Update/Delete."
            ),
        )
    return ToolResult(success=True, summary=f"UIEngine.{name} (ui, browser-side builtin):\n{json.dumps(sig, indent=2)}")


async def _execute_get_kirun_primitive(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    namespace = (params.get("namespace") or "").strip()
    name = (params.get("name") or "").strip()
    if not namespace or not name:
        return ToolResult(success=False, error="`namespace` and `name` are required")
    if namespace == "UIEngine":
        return _uiengine_primitive_result(name)
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
    description="""Add a single step to an existing function via surgical PATCH. The function's other steps stay untouched — only the new step is appended to the steps map.

Use this for additive edits ("add a step that lowercases the input"); for wholesale replacement use `save_function_from_text` instead.

IMPORTANT pre-flight:
1. `decompile_function(name="<NS>.<Func>")` first to see the existing step names + their dependency wiring. Step names must be unique within the function — picking a colliding name will fail.
2. `get_kirun_primitive(namespace="<NS>", name="<Name>")` to confirm the primitive's exact parameter names. The `params` dict you pass must match the primitive's parameter map exactly; unknown params get silently dropped by the platform.
3. After adding the step, call `save_function_from_text` on the decompiled+rewritten DSL (or call this tool's sibling `set_dependencies`) to wire your new step into the dependency chain — `add_step` does NOT auto-wire.

Parameter passing in the `params` dict:
- Raw values: `{"name": "filter", "operator": "EQUALS"}`
- Argument references: `{"input": "Arguments.x"}` (auto-coerced into an expression by the platform)
- Step references: `{"value": "Steps.previousStep.output.value"}` (also auto-coerced)
- Context references: `{"src": "Context.filter"}`

Dependencies declare event-level edges. Either `["stepName"]` (default to `.output`) or `["Steps.previousStep.output"]` (explicit). Cross-event branches (e.g. error handling) use `["Steps.tryStep.error"]`.""",
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
