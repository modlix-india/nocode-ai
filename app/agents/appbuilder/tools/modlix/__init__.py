"""Modlix tool suite — ported from modlix-mcp into the CFA's appbuilder agent.

This package holds the ~195 tools that used to live in modlix-mcp's FastMCP
server (`/Users/kirangrandhi/kiran/fincity/modlix-mcp/modlix_mcp/tools/`).
modlix-mcp was the iteration ground (because writing a Python tool + restarting
the MCP and letting Claude Code use it gives the fastest feedback loop in the
ecosystem). Now mature, they move here so the customer-facing agent at
`POST /api/ai/appbuilder/chat` can expose the full surface.

Subdivisions match the categories in the rewrite plan:
  - pages.py        page CRUD + composition (composition + composition_v2 merged)
  - components.py   add/move/remove/rename + style ops
  - kirun.py        function CRUD, server functions, page event functions,
                    function_steps, kirun DSL, kirun primitives, function_execute
  - schemas.py      schemas + storages + storage_data
  - visuals.py      screenshot + drive + preview + image_gen + image_ops + files
  - security.py     security + transports
  - infra.py        apps + themes + styles + uri_paths + cache + logs +
                    environment + notifications + connections + templates +
                    events + personalization + html_compiler

Each module exports a `TOOLS: list[ToolDefinition]` that gets aggregated into
`MODLIX_TOOLS` by the registry. Tools accept `(params, context)` per the
`app.core.tools.base.ToolExecuteFunc` signature; auth headers come from
`context["headers"]` (already populated from the caller's JWT by the request
auth middleware — no separate dev login required).

Helper modules (private, leading underscore):
  - _kirun_dsl.py     DSL compile / decompile / validate / format
  - _kirun_layout.py  step-graph auto-layout (mirrors the React editor's algo)
  - _conventions.py   platform grammar: authorities, expressions, multi-valued
                      props, parameterMap, breakpoints, identifier validators,
                      component type whitelist
  - _page_ops.py      (Phase 1.2) page read-modify-write helpers
"""

from __future__ import annotations

__all__: list[str] = []
