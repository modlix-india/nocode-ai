"""Runtime helpers — personalization (READ-only).

Ports modlix-mcp/modlix_mcp/tools/personalization.py — 3 tools.

  /api/ui/personalization. Per-user runtime UI prefs (column visibility,
  panel layout, sort orders). cxapp has 613 docs in dev; appbuilder 166.
  WRITES are intentionally absent — the runtime owns them
  (memory: feedback-storage-db-readonly applies in spirit: runtime data,
  not builder data).

The html_compiler suite from modlix-mcp is deliberately NOT ported. The
LLM can author with the granular pages + components tools (add_component,
patch_component_props, set_styles, etc.); an HTML→Modlix translator only
saves work on greenfield static pages and produces lossy structure that
still needs binding + event wiring afterward. If a bench run shows the
agent struggling with greenfield page authoring, revisit.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult


# Shared param-description constants.
_DESC_APP_CODE = "appCode; defaults to the app this session is working in"


def _client_and_headers(context: dict[str, Any]) -> tuple[Any, dict[str, str]]:
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context.get("headers") or {}


def _resolve_app_code(params: dict[str, Any], context: dict[str, Any]) -> str:
    from app.agents.appbuilder.tools._shared import resolve_app_code
    return resolve_app_code(params, context)


def _err_app_code() -> ToolResult:
    return ToolResult(success=False, error="`app_code` is required (set in context or pass explicitly).")


def _page_size(params: dict[str, Any], default: int = 100, cap: int = 1000) -> int:
    try:
        return max(1, min(int(params.get("size") or default), cap))
    except (TypeError, ValueError):
        return default


# ═════════════════════════════════════════════════════════════════════════
#  PERSONALIZATION (3 tools, READ-only)
# ═════════════════════════════════════════════════════════════════════════

_PERS_API = "/api/ui/personalization"


async def _execute_list_personalizations(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    r = await client.get(_PERS_API, headers=headers, params={"page": 0, "size": _page_size(params, 100, 1000), "appCode": ac})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    starts_with = params.get("name_starts_with")
    created_by = params.get("created_by")
    if starts_with:
        content = [p for p in content if (p.get("name") or "").startswith(starts_with)]
    if created_by:
        content = [p for p in content if str(p.get("createdBy") or "") == str(created_by)]
    rows = [{
        "name": p.get("name"), "id": p.get("id"), "version": p.get("version"),
        "clientCode": p.get("clientCode"), "createdBy": p.get("createdBy"),
        "fields": list((p.get("personalization") or {}).keys()),
    } for p in content]
    return ToolResult(
        success=True,
        summary=f"Personalizations in '{ac}' ({len(rows)}):\n{json.dumps(rows, indent=2, default=str)}",
    )


list_personalizations_tool = ToolDefinition(
    name="list_personalizations",
    description="List personalization docs in an app (name + owner + fields). Debugging-only: 'show me all personalizations of viewCustomerTable' or 'what has user 248 customized'. Do NOT use during build flows — runtime authors these.",
    parameters=[
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="name_starts_with", type="string", required=False, description="Filter by name prefix, e.g. 'viewCustomerTable'"),
        ToolParameter(name="created_by", type="string", required=False, description="Filter by owning user id"),
        ToolParameter(name="size", type="integer", required=False, default=100, description="Max rows"),
    ],
    execute=_execute_list_personalizations,
)


async def _execute_get_personalization(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
    client, headers = _client_and_headers(context)
    r = await client.get(_PERS_API, headers=headers, params={"page": 0, "size": 1, "appCode": ac, "name": name})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    if not content:
        return ToolResult(success=False, error=f"personalization '{name}' not found in app '{ac}'.")
    detail = await client.get(f"{_PERS_API}/{content[0].get('id')}", headers=headers)
    if not detail.success:
        return ToolResult(success=False, error=detail.error)
    return ToolResult(success=True, summary=json.dumps(detail.data, indent=2, default=str))


get_personalization_tool = ToolDefinition(
    name="get_personalization",
    description="Read one user's personalization choices for one component (e.g. 'viewCustomerTable248').",
    parameters=[
        ToolParameter(name="name", type="string", description="Full personalization doc name, e.g. 'viewCustomerTable248'"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ],
    execute=_execute_get_personalization,
)


async def _execute_count_personalizations(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    r = await client.get(_PERS_API, headers=headers, params={"page": 0, "size": 1000, "appCode": ac})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    total = (r.data or {}).get("totalElements", len(content)) if isinstance(r.data, dict) else len(content)
    starts_with = params.get("name_starts_with")
    if starts_with:
        n = sum(1 for p in content if (p.get("name") or "").startswith(starts_with))
        return ToolResult(success=True, summary=f"Personalizations in '{ac}' starting with '{starts_with}': {n} (page sample, total in app: {total})")
    return ToolResult(success=True, summary=f"Personalizations in '{ac}': total={total}")


count_personalizations_tool = ToolDefinition(
    name="count_personalizations",
    description="Count personalization docs for an app (cheap stat). See how heavily personalization is used — cxapp has 613, appbuilder 166, most apps few/none.",
    parameters=[
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="name_starts_with", type="string", required=False, description="Optional prefix filter for grouping by component"),
    ],
    execute=_execute_count_personalizations,
)


# ── Module export ────────────────────────────────────────────────────────


TOOLS: list[ToolDefinition] = [
    list_personalizations_tool, get_personalization_tool, count_personalizations_tool,
]
