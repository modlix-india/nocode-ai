"""Schemas + storages + storage_data — Modlix data layer tools.

Consolidates three modlix-mcp modules:
  schemas.py       → 7 tools (Kirun schema CRUD UI+core + repository discovery)
  storages.py      → 5 tools (managed data tables: schema, relations, triggers, auth)
  storage_data.py  → 4 tools (READ-ONLY Mongo debug for tenant row data)

Storage *definitions* live in /api/core/storages. Storage *data rows* live in
per-tenant Mongo DBs named `<CLIENT_CODE>_<app_code>`. The CFA NEVER writes to
those data rows (see aicontext/reference/storage_db_readonly.md). Writes happen
via server-side Kirun functions; these tools are read-only diagnostics for
"why does customer X have status=pending?" queries.

Activation: storage_data tools require `MODLIX_MONGO_URL` env var with a
connection string. Without it those four tools return a configuration error;
schemas + storages CRUD keep working.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

from . import _conventions as c


# Shared param-description constants.
_DESC_APP_CODE = "appCode; defaults to the app this session is working in"
_DESC_CLIENT_CODE = "clientCode; defaults to session"
_DESC_COMMIT_MSG = "Commit message"
_DESC_RUNTIME = "'ui' or 'core' — most app schemas live in 'core'"
_DESC_INCLUDE_BUILTINS = "Layer Kirun built-in schemas under app schemas (usually true)"


_SCHEMAS_RUNTIMES = ("ui", "core")


def _client_and_headers(context: dict[str, Any]) -> tuple[Any, dict[str, str]]:
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context.get("headers") or {}


def _resolve_app_code(params: dict[str, Any], context: dict[str, Any]) -> tuple[str, ToolResult | None]:
    from app.agents.appbuilder.tools._shared import resolve_app_code
    ac = resolve_app_code(params, context)
    if not ac:
        return "", ToolResult(success=False, error="No appCode set. Pass `app_code` or set it on the chat request.")
    return ac, None


def _resolve_client_code(params: dict[str, Any], context: dict[str, Any]) -> str:
    return params.get("client_code") or context.get("client_code", "") or ""


def _validate_runtime(runtime: str) -> str | None:
    if runtime not in _SCHEMAS_RUNTIMES:
        return f"runtime must be 'ui' or 'core', got {runtime!r}"
    return None


def _schemas_api(runtime: str) -> str:
    return f"/api/{runtime}/schemas"


_STORAGES_API = "/api/core/storages"


# ═════════════════════════════════════════════════════════════════════════
#  SCHEMAS (7 tools)
# ═════════════════════════════════════════════════════════════════════════


async def _execute_list_schemas(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    runtime = (params.get("runtime") or "core").strip()
    rerr = _validate_runtime(runtime)
    if rerr:
        return ToolResult(success=False, error=rerr)
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    try:
        size = max(1, min(int(params.get("size") or 200), 1000))
    except (TypeError, ValueError):
        size = 200
    client, headers = _client_and_headers(context)
    req: dict[str, Any] = {"page": 0, "size": size, "appCode": ac}
    if params.get("namespace"):
        req["namespace"] = params["namespace"]
    r = await client.get(_schemas_api(runtime), headers=headers, params=req)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    rows = [{"name": s.get("name"), "id": s.get("id"), "version": s.get("version"), "clientCode": s.get("clientCode")} for s in content]
    return ToolResult(success=True, summary=f"{runtime}.schemas in app '{ac}' ({len(rows)}):\n{json.dumps(rows, indent=2, default=str)}")


list_schemas_tool = ToolDefinition(
    name="list_schemas",
    description="List Kirun schemas defined in an app (UI or core runtime). Returns names + ids + versions.",
    parameters=[
        ToolParameter(name="runtime", type="string", required=False, default="core", description=_DESC_RUNTIME),
        ToolParameter(name="namespace", type="string", required=False, description="Filter by namespace (e.g. 'cxapp', 'TestUI')"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="size", type="integer", required=False, default=200, description="Max rows (capped at 1000)"),
    ],
    execute=_execute_list_schemas,
)


def _summarize_schema(s: dict[str, Any]) -> str:
    d = s.get("definition") or {}
    lines = [
        f"Schema: {d.get('namespace', s.get('namespace', '?'))}.{d.get('name', s.get('name', '?'))}",
        f"  id: {s.get('id')}",
        f"  version: {s.get('version')}",
        f"  clientCode: {s.get('clientCode')}",
        f"  type: {d.get('type')}",
    ]
    if d.get("description"):
        lines.append(f"  description: {d['description']}")
    if d.get("ref"):
        lines.append(f"  ref: {d['ref']}")
    if d.get("required"):
        lines.append(f"  required: {d['required']}")
    props = d.get("properties") or {}
    if props:
        lines.append("  properties:")
        for pname, pdef in props.items():
            ptype = pdef.get("type") if isinstance(pdef, dict) else "?"
            lines.append(f"    - {pname}: {ptype}")
    items = d.get("items")
    if items and isinstance(items, dict):
        lines.append(f"  items: {items.get('type')}")
    if d.get("enums"):
        lines.append(f"  enums: {d['enums']}")
    if d.get("$defs"):
        lines.append(f"  $defs: {list(d['$defs'].keys())}")
    if d.get("permission"):
        lines.append(f"  permission: {d['permission']}")
    return "\n".join(lines)


async def _execute_get_schema(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required (full Namespace.LocalName)")
    runtime = (params.get("runtime") or "core").strip()
    rerr = _validate_runtime(runtime)
    if rerr:
        return ToolResult(success=False, error=rerr)
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    r = await client.get(_schemas_api(runtime), headers=headers, params={"page": 0, "size": 1, "appCode": ac, "name": name})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    if not content:
        return ToolResult(success=False, error=f"Schema '{name}' not found in {runtime} for app '{ac}'.")
    s_id = content[0].get("id")
    detail = await client.get(f"{_schemas_api(runtime)}/{s_id}", headers=headers)
    if not detail.success:
        return ToolResult(success=False, error=detail.error)
    s = detail.data if isinstance(detail.data, dict) else {}
    if (params.get("include") or "summary").strip() == "full":
        return ToolResult(success=True, summary=json.dumps(s, indent=2, default=str))
    return ToolResult(success=True, summary=_summarize_schema(s))


get_schema_tool = ToolDefinition(
    name="get_schema",
    description="Read a Kirun schema by namespaced name. include='summary' (default: type + property list) or 'full' (entire definition).",
    parameters=[
        ToolParameter(name="name", type="string", description="Namespace.LocalName (e.g. 'cxapp.Customer')"),
        ToolParameter(name="runtime", type="string", required=False, default="core", description=_DESC_RUNTIME),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="include", type="string", required=False, default="summary", description="summary | full"),
    ],
    execute=_execute_get_schema,
)


async def _execute_create_schema(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    namespace = (params.get("namespace") or "").strip()
    schema = params.get("schema")
    runtime = (params.get("runtime") or "core").strip()
    rerr = _validate_runtime(runtime)
    if rerr:
        return ToolResult(success=False, error=rerr)
    if not isinstance(schema, dict):
        return ToolResult(success=False, error="`schema` (dict) is required")
    err = c.validate_simple_name(name)
    if err:
        return ToolResult(success=False, error=err)
    if namespace.startswith("System"):
        return ToolResult(success=False, error=f"Namespace '{namespace}' is reserved.")
    ne = c.validate_namespaced_name(namespace)
    if ne:
        return ToolResult(success=False, error=f"namespace — {ne}")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    cc = _resolve_client_code(params, context)
    definition = {**schema, "name": name, "namespace": namespace}
    if isinstance(definition.get("type"), str):
        definition["type"] = [definition["type"]]
    envelope = {
        "name": f"{namespace}.{name}", "appCode": ac, "clientCode": cc,
        "message": params.get("message") or "Created schema via CFA",
        "definition": definition,
    }
    client, headers = _client_and_headers(context)
    r = await client.post(_schemas_api(runtime), headers=headers, json=envelope)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    sid = (r.data or {}).get("id", "?") if isinstance(r.data, dict) else "?"
    return ToolResult(success=True, summary=f"Created {runtime} schema '{namespace}.{name}' (id={sid}).")


create_schema_tool = ToolDefinition(
    name="create_schema",
    description="Create a new Kirun schema. The body must include `type` (list of SchemaType: INTEGER/LONG/FLOAT/DOUBLE/STRING/BOOLEAN/NULL/OBJECT/ARRAY) plus optional properties, items, required, enums, $defs, ref, etc.",
    parameters=[
        ToolParameter(name="name", type="string", description="Local schema name (e.g. 'Customer')"),
        ToolParameter(name="namespace", type="string", description="Namespace (e.g. 'cxapp'); System.* is reserved"),
        ToolParameter(name="schema", type="object", description="Kirun schema body (type + properties/items/etc.)"),
        ToolParameter(name="runtime", type="string", required=False, default="core", description=_DESC_RUNTIME),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description=_DESC_CLIENT_CODE),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG),
    ],
    execute=_execute_create_schema,
)


async def _execute_update_schema(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    schema = params.get("schema")
    if not name or not isinstance(schema, dict):
        return ToolResult(success=False, error="`name` and `schema` (dict) are required")
    runtime = (params.get("runtime") or "core").strip()
    rerr = _validate_runtime(runtime)
    if rerr:
        return ToolResult(success=False, error=rerr)
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    r = await client.get(_schemas_api(runtime), headers=headers, params={"page": 0, "size": 1, "appCode": ac, "name": name})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    if not content:
        return ToolResult(success=False, error=f"Schema '{name}' not found in {runtime} for app '{ac}'.")
    s_id = content[0].get("id")
    detail = await client.get(f"{_schemas_api(runtime)}/{s_id}", headers=headers)
    if not detail.success:
        return ToolResult(success=False, error=detail.error)
    existing = dict(detail.data) if isinstance(detail.data, dict) else {}
    if isinstance(schema.get("type"), str):
        schema["type"] = [schema["type"]]
    existing_def = existing.get("definition") or {}
    new_def = {
        **schema,
        "name": existing_def.get("name") or schema.get("name"),
        "namespace": existing_def.get("namespace") or schema.get("namespace"),
    }
    existing["definition"] = new_def
    existing["message"] = params.get("message") or "Updated schema via CFA"
    save = await client.put(f"{_schemas_api(runtime)}/{s_id}", headers=headers, json=existing)
    if not save.success:
        return ToolResult(success=False, error=save.error)
    return ToolResult(success=True, summary=f"Updated {runtime} schema '{name}'.")


update_schema_tool = ToolDefinition(
    name="update_schema",
    description="Replace a schema's body. Preserves id, name, namespace, clientCode.",
    parameters=[
        ToolParameter(name="name", type="string", description="Namespace.LocalName"),
        ToolParameter(name="schema", type="object", description="Replacement schema body"),
        ToolParameter(name="runtime", type="string", required=False, default="core", description=_DESC_RUNTIME),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG),
    ],
    execute=_execute_update_schema,
)


async def _execute_delete_schema(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    runtime = (params.get("runtime") or "core").strip()
    rerr = _validate_runtime(runtime)
    if rerr:
        return ToolResult(success=False, error=rerr)
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    r = await client.get(_schemas_api(runtime), headers=headers, params={"page": 0, "size": 1, "appCode": ac, "name": name})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    if not content:
        return ToolResult(success=False, error=f"Schema '{name}' not found.")
    s_id = content[0].get("id")
    d = await client.delete(f"{_schemas_api(runtime)}/{s_id}", headers=headers)
    if not d.success:
        return ToolResult(success=False, error=d.error)
    return ToolResult(success=True, summary=f"Deleted {runtime} schema '{name}' (id={s_id}).")


delete_schema_tool = ToolDefinition(
    name="delete_schema",
    description="Delete a Kirun schema. DESTRUCTIVE — backend rejects if any function/storage/uri still references it.",
    parameters=[
        ToolParameter(name="name", type="string", description="Namespace.LocalName"),
        ToolParameter(name="runtime", type="string", required=False, default="core", description=_DESC_RUNTIME),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ],
    execute=_execute_delete_schema,
)


async def _execute_find_schema(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    namespace = (params.get("namespace") or "").strip()
    name = (params.get("name") or "").strip()
    if not namespace or not name:
        return ToolResult(success=False, error="`namespace` and `name` are required")
    runtime = (params.get("runtime") or "core").strip()
    rerr = _validate_runtime(runtime)
    if rerr:
        return ToolResult(success=False, error=rerr)
    ac, _ = _resolve_app_code(params, context)
    cc = _resolve_client_code(params, context)
    client, headers = _client_and_headers(context)
    req: dict[str, Any] = {
        "namespace": namespace, "name": name,
        "includeKIRunRepos": "true" if params.get("include_builtins", True) else "false",
    }
    if ac:
        req["appCode"] = ac
    if cc:
        req["clientCode"] = cc
    r = await client.get(f"{_schemas_api(runtime)}/repositoryFind", headers=headers, params=req)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Resolved {namespace}.{name} ({runtime}):\n{json.dumps(r.data, indent=2, default=str)}")


find_schema_tool = ToolDefinition(
    name="find_schema",
    description="Resolve a schema by namespace+name through the platform's repository chain (SYSTEM → app → client overrides + built-ins). Use to see what shape a function/storage's schema ref actually resolves to.",
    parameters=[
        ToolParameter(name="namespace", type="string", description="Schema namespace (e.g. 'cxapp', 'System')"),
        ToolParameter(name="name", type="string", description="Schema name within the namespace"),
        ToolParameter(name="runtime", type="string", required=False, default="core", description=_DESC_RUNTIME),
        ToolParameter(name="include_builtins", type="boolean", required=False, default=True, description=_DESC_INCLUDE_BUILTINS),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description=_DESC_CLIENT_CODE),
    ],
    execute=_execute_find_schema,
)


async def _execute_filter_schemas(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    filter_re = params.get("filter") or ".*"
    runtime = (params.get("runtime") or "core").strip()
    rerr = _validate_runtime(runtime)
    if rerr:
        return ToolResult(success=False, error=rerr)
    ac, _ = _resolve_app_code(params, context)
    cc = _resolve_client_code(params, context)
    client, headers = _client_and_headers(context)
    req: dict[str, Any] = {
        "filter": filter_re,
        "includeKIRunRepos": "true" if params.get("include_builtins", True) else "false",
    }
    if ac:
        req["appCode"] = ac
    if cc:
        req["clientCode"] = cc
    r = await client.get(f"{_schemas_api(runtime)}/repositoryFilter", headers=headers, params=req)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    names = sorted(r.data) if isinstance(r.data, list) else []
    return ToolResult(success=True, summary=f"{runtime} schemas matching '{filter_re}' ({len(names)}):\n" + "\n".join(names))


filter_schemas_tool = ToolDefinition(
    name="filter_schemas",
    description="Search schemas by regex through the live repository (overrides + built-ins). Different from list_schemas — walks the full resolution chain matching the runtime view.",
    parameters=[
        ToolParameter(name="filter", type="string", required=False, default=".*", description="Regex over fully-qualified names"),
        ToolParameter(name="runtime", type="string", required=False, default="core", description=_DESC_RUNTIME),
        ToolParameter(name="include_builtins", type="boolean", required=False, default=True, description=_DESC_INCLUDE_BUILTINS),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description=_DESC_CLIENT_CODE),
    ],
    execute=_execute_filter_schemas,
)


# ═════════════════════════════════════════════════════════════════════════
#  STORAGES (5 tools)
# ═════════════════════════════════════════════════════════════════════════


def _validate_storage_schema(schema: dict[str, Any]) -> str | None:
    if not isinstance(schema, dict) or not schema:
        return "schema must be a non-empty object"
    has_ref = bool(schema.get("ref"))
    has_props = bool(schema.get("properties"))
    if has_ref and has_props:
        return "schema has BOTH 'ref' and 'properties' — mutually exclusive."
    if not has_ref and not has_props:
        return "schema needs either 'ref' or 'properties'."
    if has_props:
        t = schema.get("type")
        if isinstance(t, str):
            t = [t]
        if not isinstance(t, list) or "OBJECT" not in t:
            return "inline schema must declare type=['OBJECT'] (storage rows are objects)"
    return None


def _summarize_storage(s: dict[str, Any]) -> str:
    schema = s.get("schema") or {}
    props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
    relations = s.get("relations") or {}
    triggers = s.get("triggers") or {}
    lines = [
        f"Storage: {s.get('name', '?')}",
        f"  id: {s.get('id')}",
        f"  version: {s.get('version')}",
        f"  clientCode: {s.get('clientCode')}",
        f"  flags: audited={s.get('isAudited')} versioned={s.get('isVersioned')} "
        f"onlyThruKIRun={s.get('onlyThruKIRun')} generateEvents={s.get('generateEvents')}",
    ]
    for label, field in (("createAuth", "createAuth"), ("readAuth", "readAuth"),
                          ("updateAuth", "updateAuth"), ("deleteAuth", "deleteAuth")):
        if s.get(field):
            lines.append(f"  {label}: {s[field]}")
    lines.append(f"  schema.type: {schema.get('type')}")
    if props:
        lines.append("  schema.properties:")
        for pname, pdef in props.items():
            lines.append(f"    - {pname}: {pdef.get('type') if isinstance(pdef, dict) else '?'}")
    if relations:
        lines.append("  relations:")
        for rname, rdef in relations.items():
            if isinstance(rdef, dict):
                lines.append(f"    - {rname}: {rdef.get('relationType','?')} → {rdef.get('storageName','?')}")
    if triggers:
        lines.append("  triggers:")
        for phase, fns in triggers.items():
            if fns:
                lines.append(f"    {phase}: {fns}")
    return "\n".join(lines)


async def _execute_list_storages(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    try:
        size = max(1, min(int(params.get("size") or 200), 1000))
    except (TypeError, ValueError):
        size = 200
    client, headers = _client_and_headers(context)
    r = await client.get(_STORAGES_API, headers=headers, params={"page": 0, "size": size, "appCode": ac})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    rows = [{
        "name": s.get("name"), "id": s.get("id"), "version": s.get("version"),
        "clientCode": s.get("clientCode"),
        "properties": len(((s.get("schema") or {}).get("properties") or {})),
        "relations": len(s.get("relations") or {}),
        "triggers": sum(len(v or []) for v in (s.get("triggers") or {}).values()),
        "audited": s.get("isAudited"),
        "versioned": s.get("isVersioned"),
    } for s in content]
    return ToolResult(success=True, summary=f"Storages in app '{ac}' ({len(rows)}):\n{json.dumps(rows, indent=2, default=str)}")


list_storages_tool = ToolDefinition(
    name="list_storages",
    description="List storages (data tables) defined in an app with row counts of schema columns / relations / triggers.",
    parameters=[
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="size", type="integer", required=False, default=200, description="Max rows"),
    ],
    execute=_execute_list_storages,
)


async def _execute_get_storage(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    r = await client.get(_STORAGES_API, headers=headers, params={"page": 0, "size": 1, "appCode": ac, "name": name})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    if not content:
        return ToolResult(success=False, error=f"Storage '{name}' not found in app '{ac}'.")
    s_id = content[0].get("id")
    detail = await client.get(f"{_STORAGES_API}/{s_id}", headers=headers)
    if not detail.success:
        return ToolResult(success=False, error=detail.error)
    s = detail.data if isinstance(detail.data, dict) else {}
    if (params.get("include") or "summary").strip() == "full":
        return ToolResult(success=True, summary=json.dumps(s, indent=2, default=str))
    return ToolResult(success=True, summary=_summarize_storage(s))


get_storage_tool = ToolDefinition(
    name="get_storage",
    description="Read a storage definition. include='summary' (default: schema cols, relations, triggers, auth, flags) or 'full' (entire doc).",
    parameters=[
        ToolParameter(name="name", type="string", description="Storage name"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="include", type="string", required=False, default="summary", description="summary | full"),
    ],
    execute=_execute_get_storage,
)


def _validate_auths(create_auth, read_auth, update_auth, delete_auth) -> str | None:
    for label, auth in (("create_auth", create_auth), ("read_auth", read_auth),
                         ("update_auth", update_auth), ("delete_auth", delete_auth)):
        if auth:
            ae = c.validate_authority(auth)
            if ae:
                return f"{label} — {ae}"
    return None


async def _execute_create_storage(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    schema = params.get("schema")
    nerr = c.validate_simple_name(name)
    if nerr:
        return ToolResult(success=False, error=nerr)
    if not isinstance(schema, dict):
        return ToolResult(success=False, error="`schema` (dict) is required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    cc = _resolve_client_code(params, context)

    auth_err = _validate_auths(
        params.get("create_auth"), params.get("read_auth"),
        params.get("update_auth"), params.get("delete_auth"),
    )
    if auth_err:
        return ToolResult(success=False, error=auth_err)
    sch_err = _validate_storage_schema(schema)
    if sch_err:
        return ToolResult(success=False, error=sch_err)
    if isinstance(schema.get("type"), str):
        schema["type"] = [schema["type"]]

    body: dict[str, Any] = {
        "name": name, "appCode": ac, "clientCode": cc, "schema": schema,
        "isAudited": bool(params.get("is_audited", True)),
        "isVersioned": bool(params.get("is_versioned", False)),
        "onlyThruKIRun": bool(params.get("only_thru_kirun", False)),
        "generateEvents": bool(params.get("generate_events", False)),
        "message": params.get("message") or "Created storage via CFA",
    }
    for opt_key, body_key in (
        ("relations", "relations"), ("triggers", "triggers"),
        ("text_index_fields", "textIndexFields"), ("indexes", "indexes"),
        ("create_auth", "createAuth"), ("read_auth", "readAuth"),
        ("update_auth", "updateAuth"), ("delete_auth", "deleteAuth"),
        ("title", "title"), ("description", "description"),
        ("field_definition_map", "fieldDefinitionMap"),
    ):
        v = params.get(opt_key)
        if v:
            body[body_key] = v

    client, headers = _client_and_headers(context)
    r = await client.post(_STORAGES_API, headers=headers, json=body)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    sid = (r.data or {}).get("id", "?") if isinstance(r.data, dict) else "?"
    return ToolResult(success=True, summary=f"Created storage '{name}' (id={sid}).")


_DESC_AUTHORITY_FMT = "Authority string (Authorities.[APPCODE.]<Permission>)"

create_storage_tool = ToolDefinition(
    name="create_storage",
    description="Create a storage (data-table definition). schema describes ONE row's shape (Kirun OBJECT). Optional: relations, triggers, per-op auth gates, indexes, fieldDefinitionMap (form rendering metadata).",
    parameters=[
        ToolParameter(name="name", type="string", description="Storage name (letters/digits, e.g. 'customer')"),
        ToolParameter(name="schema", type="object", description="Kirun OBJECT schema: {type:'OBJECT', properties:{...}}"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description=_DESC_CLIENT_CODE),
        ToolParameter(name="relations", type="object", required=False, description="Foreign-key relations map"),
        ToolParameter(name="triggers", type="object", required=False, description="Lifecycle hooks {BEFORE_CREATE: [...], ...}"),
        ToolParameter(name="create_auth", type="string", required=False, description=f"{_DESC_AUTHORITY_FMT} for INSERT"),
        ToolParameter(name="read_auth", type="string", required=False, description=f"{_DESC_AUTHORITY_FMT} for READ"),
        ToolParameter(name="update_auth", type="string", required=False, description=f"{_DESC_AUTHORITY_FMT} for UPDATE"),
        ToolParameter(name="delete_auth", type="string", required=False, description=f"{_DESC_AUTHORITY_FMT} for DELETE"),
        ToolParameter(name="is_audited", type="boolean", required=False, default=True, description="Track created/updated by per row"),
        ToolParameter(name="is_versioned", type="boolean", required=False, default=False, description="Keep version history"),
        ToolParameter(
            name="only_thru_kirun", type="boolean", required=False, default=False,
            description=(
                "Refuse the raw data API entirely (READS as well as writes) unless the call comes "
                "from inside a KIRun execution — AppDataService.getStorageWithKIRunValidation "
                "returns empty, which surfaces as 404. Set TRUE: pages must reach data through "
                "api/core/function/execute/... , never api/core/data/<storage>."
            ),
        ),
        ToolParameter(name="generate_events", type="boolean", required=False, default=False, description="Emit eventDefinitions on row mutations"),
        ToolParameter(name="text_index_fields", type="array", required=False, description="Fields to enable FTS on", items={"type": "string"}),
        ToolParameter(name="indexes", type="object", required=False, description="Index definitions"),
        ToolParameter(name="title", type="string", required=False, description="Human-readable display title"),
        ToolParameter(name="description", type="string", required=False, description="Short description"),
        ToolParameter(name="field_definition_map", type="object", required=False, description="Per-field form metadata (label, placeholder, inputType, validation)"),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG),
    ],
    execute=_execute_create_storage,
)


async def _execute_update_storage(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    r = await client.get(_STORAGES_API, headers=headers, params={"page": 0, "size": 1, "appCode": ac, "name": name})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    if not content:
        return ToolResult(success=False, error=f"Storage '{name}' not found in app '{ac}'.")
    s_id = content[0].get("id")
    detail = await client.get(f"{_STORAGES_API}/{s_id}", headers=headers)
    if not detail.success:
        return ToolResult(success=False, error=detail.error)
    s = detail.data if isinstance(detail.data, dict) else {}

    changed: list[str] = []
    schema = params.get("schema")
    if schema is not None:
        if not isinstance(schema, dict):
            return ToolResult(success=False, error="`schema` must be a dict")
        se = _validate_storage_schema(schema)
        if se:
            return ToolResult(success=False, error=se)
        if isinstance(schema.get("type"), str):
            schema["type"] = [schema["type"]]
        s["schema"] = schema
        changed.append("schema")
    for opt_key, body_key in (
        ("relations", "relations"), ("triggers", "triggers"),
        ("indexes", "indexes"), ("text_index_fields", "textIndexFields"),
        ("title", "title"), ("description", "description"),
        ("field_definition_map", "fieldDefinitionMap"),
    ):
        v = params.get(opt_key)
        if v is not None:
            s[body_key] = v
            changed.append(body_key)
    for opt_key, body_key in (
        ("create_auth", "createAuth"), ("read_auth", "readAuth"),
        ("update_auth", "updateAuth"), ("delete_auth", "deleteAuth"),
    ):
        v = params.get(opt_key)
        if v is not None:
            ae = c.validate_authority(v)
            if ae:
                return ToolResult(success=False, error=f"{opt_key} — {ae}")
            s[body_key] = v
            changed.append(body_key)
    for opt_key, body_key in (
        ("is_audited", "isAudited"), ("is_versioned", "isVersioned"),
        ("only_thru_kirun", "onlyThruKIRun"), ("generate_events", "generateEvents"),
    ):
        v = params.get(opt_key)
        if v is not None:
            s[body_key] = bool(v)
            changed.append(body_key)
    if not changed:
        return ToolResult(success=True, summary="No-op: nothing to update.")
    s["message"] = params.get("message") or "Updated storage via CFA"
    save = await client.put(f"{_STORAGES_API}/{s_id}", headers=headers, json=s)
    if not save.success:
        return ToolResult(success=False, error=save.error)
    return ToolResult(success=True, summary=f"Updated storage '{name}': {', '.join(changed)}.")


update_storage_tool = ToolDefinition(
    name="update_storage",
    description="Update a storage definition. Each non-None argument replaces that field; pass only what you want to change.",
    parameters=[
        ToolParameter(name="name", type="string", description="Storage name to update"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="schema", type="object", required=False, description="Replace schema (Kirun OBJECT)"),
        ToolParameter(name="relations", type="object", required=False, description="Replace relations map"),
        ToolParameter(name="triggers", type="object", required=False, description="Replace triggers"),
        ToolParameter(name="indexes", type="object", required=False, description="Replace indexes"),
        ToolParameter(name="text_index_fields", type="array", required=False, description="Replace FTS fields", items={"type": "string"}),
        ToolParameter(name="create_auth", type="string", required=False, description=f"Replace {_DESC_AUTHORITY_FMT}"),
        ToolParameter(name="read_auth", type="string", required=False, description=f"Replace {_DESC_AUTHORITY_FMT}"),
        ToolParameter(name="update_auth", type="string", required=False, description=f"Replace {_DESC_AUTHORITY_FMT}"),
        ToolParameter(name="delete_auth", type="string", required=False, description=f"Replace {_DESC_AUTHORITY_FMT}"),
        ToolParameter(name="is_audited", type="boolean", required=False, description="Toggle audit"),
        ToolParameter(name="is_versioned", type="boolean", required=False, description="Toggle versioning"),
        ToolParameter(name="only_thru_kirun", type="boolean", required=False, description="Toggle REST-write block"),
        ToolParameter(name="generate_events", type="boolean", required=False, description="Toggle event emission"),
        ToolParameter(name="title", type="string", required=False, description="Replace title"),
        ToolParameter(name="description", type="string", required=False, description="Replace description"),
        ToolParameter(name="field_definition_map", type="object", required=False, description="Replace form metadata"),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG),
    ],
    execute=_execute_update_storage,
)


async def _execute_delete_storage(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    r = await client.get(_STORAGES_API, headers=headers, params={"page": 0, "size": 1, "appCode": ac, "name": name})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    if not content:
        return ToolResult(success=False, error=f"Storage '{name}' not found in app '{ac}'.")
    s_id = content[0].get("id")
    d = await client.delete(f"{_STORAGES_API}/{s_id}", headers=headers)
    if not d.success:
        return ToolResult(success=False, error=d.error)
    return ToolResult(success=True, summary=f"Deleted storage '{name}' (id={s_id}).")


delete_storage_tool = ToolDefinition(
    name="delete_storage",
    description="Delete a storage definition. DESTRUCTIVE — per-tenant data rows are NOT deleted but become unreachable through the platform once the definition is gone. Backend rejects if other entities still reference it.",
    parameters=[
        ToolParameter(name="name", type="string", description="Storage name to delete"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ],
    execute=_execute_delete_storage,
)


# ═════════════════════════════════════════════════════════════════════════
#  STORAGE DATA — READ-ONLY Mongo debug (4 tools)
# ═════════════════════════════════════════════════════════════════════════
#
# These tools speak Mongo directly. The CFA NEVER writes through them — only
# reads for "why does row X look like this" debugging. Writes happen via the
# platform's server-side Kirun functions. Activation requires the
# MODLIX_MONGO_URL env var; without it the tools surface a clear config error.


_mongo_client = None
_mongo_failure: str | None = None


def _get_mongo() -> tuple[Any, str | None]:
    """Lazy-create the pymongo MongoClient. Returns (client, error)."""
    global _mongo_client, _mongo_failure
    if _mongo_client is not None:
        return _mongo_client, None
    if _mongo_failure:
        return None, _mongo_failure
    from app.config import settings
    url = getattr(settings, "MONGO_URL", "") or getattr(settings, "MODLIX_MONGO_URL", "") or ""
    if not url:
        _mongo_failure = (
            "Storage data debug tools require MONGO_URL / MODLIX_MONGO_URL to be set "
            "(e.g. 'mongodb://admin:****@dev-mongo:27017/?authSource=admin'). "
            "Without it, only definition-level storage CRUD works."
        )
        return None, _mongo_failure
    try:
        from pymongo import MongoClient  # type: ignore[import-untyped]
    except ImportError:
        _mongo_failure = "pymongo not installed; add to requirements.txt to use storage_data tools"
        return None, _mongo_failure
    try:
        _mongo_client = MongoClient(url, serverSelectionTimeoutMS=5000)
        _mongo_client.admin.command("ping")
    except Exception as e:  # noqa: BLE001
        _mongo_failure = f"Mongo connection failed: {type(e).__name__}: {e}"
        return None, _mongo_failure
    return _mongo_client, None


def _db_name(client_code: str, app_code: str) -> str:
    return f"{client_code}_{app_code}"


_DESC_TENANT_CLIENT = "Tenant client code (e.g. 'CITYV', 'ABDUL1')"
_DESC_TENANT_APP = "App code (e.g. 'cxapp')"
_DESC_STORAGE = "Storage name (collection name in the tenant DB)"


async def _execute_list_storage_collections(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client_code = (params.get("client_code") or "").strip()
    app_code = (params.get("app_code") or "").strip()
    if not client_code or not app_code:
        return ToolResult(success=False, error="`client_code` and `app_code` are required (this is the TENANT DB lookup, not the JWT-scoped clientCode/appCode)")
    m, err = _get_mongo()
    if err:
        return ToolResult(success=False, error=err)
    db = m.get_database(_db_name(client_code, app_code))
    try:
        cols = sorted(db.list_collection_names())
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"{type(e).__name__}: {e}")
    return ToolResult(success=True, summary=f"Collections in {_db_name(client_code, app_code)} ({len(cols)}):\n" + "\n".join(cols))


list_storage_collections_tool = ToolDefinition(
    name="list_storage_collections",
    description="(READ-ONLY) List row collections in one tenant's storage DB. Requires MODLIX_MONGO_URL.",
    parameters=[
        ToolParameter(name="client_code", type="string", description=_DESC_TENANT_CLIENT),
        ToolParameter(name="app_code", type="string", description=_DESC_TENANT_APP),
    ],
    execute=_execute_list_storage_collections,
)


async def _execute_count_storage_rows(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client_code = (params.get("client_code") or "").strip()
    app_code = (params.get("app_code") or "").strip()
    storage_name = (params.get("storage_name") or "").strip()
    if not all([client_code, app_code, storage_name]):
        return ToolResult(success=False, error="`client_code`, `app_code`, `storage_name` are required")
    m, err = _get_mongo()
    if err:
        return ToolResult(success=False, error=err)
    db = m.get_database(_db_name(client_code, app_code))
    try:
        n = db[storage_name].count_documents(params.get("filter") or {})
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"{type(e).__name__}: {e}")
    suffix = " matching filter" if params.get("filter") else ""
    return ToolResult(success=True, summary=f"{_db_name(client_code, app_code)}.{storage_name}: {n} rows{suffix}")


count_storage_rows_tool = ToolDefinition(
    name="count_storage_rows",
    description="(READ-ONLY) Count rows in a tenant's storage collection. Cheap — uses Mongo's count_documents.",
    parameters=[
        ToolParameter(name="client_code", type="string", description=_DESC_TENANT_CLIENT),
        ToolParameter(name="app_code", type="string", description=_DESC_TENANT_APP),
        ToolParameter(name="storage_name", type="string", description=_DESC_STORAGE),
        ToolParameter(name="filter", type="object", required=False, description="Mongo filter dict; omit for total count"),
    ],
    execute=_execute_count_storage_rows,
)


async def _execute_query_storage_rows(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client_code = (params.get("client_code") or "").strip()
    app_code = (params.get("app_code") or "").strip()
    storage_name = (params.get("storage_name") or "").strip()
    if not all([client_code, app_code, storage_name]):
        return ToolResult(success=False, error="`client_code`, `app_code`, `storage_name` are required")
    try:
        limit = max(1, min(int(params.get("limit") or 20), 200))
    except (TypeError, ValueError):
        limit = 20
    m, err = _get_mongo()
    if err:
        return ToolResult(success=False, error=err)
    db = m.get_database(_db_name(client_code, app_code))
    try:
        filt = params.get("filter") or {}
        proj = params.get("projection")
        if proj:
            cursor = db[storage_name].find(filt, {f: 1 for f in proj})
        else:
            cursor = db[storage_name].find(filt)
        sort_spec = params.get("sort")
        if sort_spec:
            cursor = cursor.sort(list(sort_spec.items()))
        rows = list(cursor.limit(limit))
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"{type(e).__name__}: {e}")
    for row in rows:
        if "_id" in row:
            row["_id"] = str(row["_id"])
    return ToolResult(success=True, summary=f"{_db_name(client_code, app_code)}.{storage_name} ({len(rows)} rows, limit={limit}):\n{json.dumps(rows, indent=2, default=str)}")


query_storage_rows_tool = ToolDefinition(
    name="query_storage_rows",
    description="(READ-ONLY) Read rows from a tenant's storage collection with filter / projection / sort / limit. Use for data-state debugging.",
    parameters=[
        ToolParameter(name="client_code", type="string", description=_DESC_TENANT_CLIENT),
        ToolParameter(name="app_code", type="string", description=_DESC_TENANT_APP),
        ToolParameter(name="storage_name", type="string", description=_DESC_STORAGE),
        ToolParameter(name="filter", type="object", required=False, description="Mongo filter dict"),
        ToolParameter(name="projection", type="array", required=False, description="Field names to include; omit for full row", items={"type": "string"}),
        ToolParameter(name="limit", type="integer", required=False, default=20, description="Max rows (capped at 200)"),
        ToolParameter(name="sort", type="object", required=False, description="Sort spec, e.g. {createdAt: -1}"),
    ],
    execute=_execute_query_storage_rows,
)


async def _execute_get_storage_row(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client_code = (params.get("client_code") or "").strip()
    app_code = (params.get("app_code") or "").strip()
    storage_name = (params.get("storage_name") or "").strip()
    row_id = (params.get("row_id") or "").strip()
    if not all([client_code, app_code, storage_name, row_id]):
        return ToolResult(success=False, error="`client_code`, `app_code`, `storage_name`, `row_id` are required")
    m, err = _get_mongo()
    if err:
        return ToolResult(success=False, error=err)
    db = m.get_database(_db_name(client_code, app_code))
    try:
        from bson import ObjectId  # type: ignore[import-untyped]
        try:
            row = db[storage_name].find_one({"_id": ObjectId(row_id)})
        except Exception:  # noqa: BLE001
            row = db[storage_name].find_one({"_id": row_id})
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"{type(e).__name__}: {e}")
    if row is None:
        return ToolResult(success=False, error=f"Row '{row_id}' not found in {_db_name(client_code, app_code)}.{storage_name}.")
    if "_id" in row:
        row["_id"] = str(row["_id"])
    return ToolResult(success=True, summary=json.dumps(row, indent=2, default=str))


get_storage_row_tool = ToolDefinition(
    name="get_storage_row",
    description="(READ-ONLY) Fetch one specific row by _id from a tenant's storage collection.",
    parameters=[
        ToolParameter(name="client_code", type="string", description=_DESC_TENANT_CLIENT),
        ToolParameter(name="app_code", type="string", description=_DESC_TENANT_APP),
        ToolParameter(name="storage_name", type="string", description=_DESC_STORAGE),
        ToolParameter(name="row_id", type="string", description="Mongo _id (string)"),
    ],
    execute=_execute_get_storage_row,
)


# ── Module export ────────────────────────────────────────────────────────


TOOLS: list[ToolDefinition] = [
    # Schemas (7)
    list_schemas_tool,
    get_schema_tool,
    create_schema_tool,
    update_schema_tool,
    delete_schema_tool,
    find_schema_tool,
    filter_schemas_tool,
    # Storages (5)
    list_storages_tool,
    get_storage_tool,
    create_storage_tool,
    update_storage_tool,
    delete_storage_tool,
    # Storage data — READ-ONLY (5; read_storage_rows appended at end of file)
    list_storage_collections_tool,
    count_storage_rows_tool,
    query_storage_rows_tool,
    get_storage_row_tool,
]


# ── read_storage_rows — inspect rows through the platform, not Mongo ─────────
#
# `query_storage_rows` / `count_storage_rows` above talk to Mongo directly.
# That works only where a Mongo port is reachable (local dev), bypasses every
# platform authorization check, and knows nothing about storage semantics.
#
# This tool goes through core instead: GET api/core/internal/data/<storage>,
# a cluster-only READ-ONLY route (nginx blocks public /internal/**). It carries
# the KIRun context marker, so it can also read storages marked
# `only_thru_kirun=true` — which every generated app's storages SHOULD be, so
# that pages are forced through api/core/function/execute/... instead of the
# raw data API. Per-storage readAuth still applies; this only relaxes the
# KIRun gate, never the authority check.

_INTERNAL_DATA_PATH = "/api/core/internal/data"


async def _execute_read_storage_rows(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    storage_name = (params.get("storage_name") or "").strip()
    if not storage_name:
        return ToolResult(success=False, error="`storage_name` is required")
    from app.agents.appbuilder.tools._shared import resolve_app_code
    app_code = resolve_app_code(params, context)
    if not app_code:
        return ToolResult(success=False, error="No appCode set. Pass `app_code` or set it on the chat request.")
    client_code = (params.get("client_code") or context.get("client_code") or "SYSTEM").strip()
    try:
        size = max(1, min(int(params.get("size") or 20), 200))
    except (TypeError, ValueError):
        size = 20
    try:
        page_no = max(0, int(params.get("page") or 0))
    except (TypeError, ValueError):
        page_no = 0

    client, headers = _client_and_headers(context)
    req_headers = dict(headers)
    # The route is permitAll so Spring does not reject it before the token is
    # read, but readPage still needs a security context — keep the bearer token.
    req_headers["appCode"] = app_code
    req_headers["clientCode"] = client_code

    query: dict[str, Any] = {"page": page_no, "size": size, "count": "true"}
    for field, value in (params.get("filter") or {}).items():
        query[str(field)] = value

    r = await client.get(f"{_INTERNAL_DATA_PATH}/{storage_name}", headers=req_headers, params=query)
    if not r.success:
        return ToolResult(
            success=False,
            error=(
                f"{r.error}\n\nIf this is a 404 on the ROUTE (not the storage), core may predate "
                f"api/core/internal/data — rebuild and restart core. If it is 403, the storage's "
                f"readAuth denies this caller."
            ),
        )
    data = r.data if isinstance(r.data, dict) else {}
    rows = data.get("content") or []
    total = data.get("totalElements", len(rows))
    return ToolResult(
        success=True,
        summary=(
            f"{app_code}/{client_code} storage '{storage_name}': {len(rows)} of {total} row(s) "
            f"(page {page_no}, size {size})\n{json.dumps(rows, indent=2, default=str)}"
        ),
        data={"content": rows, "totalElements": total},
    )


read_storage_rows_tool = ToolDefinition(
    name="read_storage_rows",
    description=(
        "(READ-ONLY) Read rows of a storage THROUGH the platform (core's cluster-only "
        "api/core/internal/data route). Prefer this over query_storage_rows: it works wherever the "
        "gateway is reachable rather than needing a Mongo port, it honours the storage's readAuth, "
        "and it can read storages marked only_thru_kirun=true. Use it to verify what a page actually "
        "wrote. Writes are deliberately not offered here — they must go through "
        "api/core/function/execute/... so triggers, validation and events run."
    ),
    parameters=[
        ToolParameter(name="storage_name", type="string", description=_DESC_STORAGE),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description="Tenant client code; defaults to the session client"),
        ToolParameter(name="filter", type="object", required=False, description="Field equality filters, e.g. {status: 'PENDING'}"),
        ToolParameter(name="page", type="integer", required=False, default=0, description="0-based page index"),
        ToolParameter(name="size", type="integer", required=False, default=20, description="Rows per page (capped at 200)"),
    ],
    execute=_execute_read_storage_rows,
)


# Defined after TOOLS, so register it here.
TOOLS.append(read_storage_rows_tool)
