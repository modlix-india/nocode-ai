"""Tool registry — imports and exports ALL appbuilder tools.

Used by AppBuilderAgent.__init__() to register all available tools.

Surfaces (transitional — the legacy CRUD/version/api-catalog tools coexist
with the modlix port until Phase 3's deferred-tool model is verified end-to-
end; only then do they retire):

  - LEGACY_TOOLS    — the old 10-tool surface (CRUD + version + API catalog)
                       + TOOL_ROUTER for the current tool-of-tools pattern
  - MODLIX_TOOLS    — the ported modlix-mcp tools (one module per category)
  - META_TOOLS      — search_tools + get_tool_schema (deferred-tool surface)
  - WORKSPACE_TOOLS — code_read / code_grep / code_glob / code_ls /
                       code_list_repos for the local code workspace
  - KB_APP_TOOLS    — kb_app_get/_search/_history/_list_sections +
                       propose_kb_update + commit_kb_update
  - ALL_TOOLS       — the union (legacy still active by default; the agent
                       loop picks which surface to expose per turn)

When Phase 3 lands, the deferred surface becomes the default; the legacy
TOOL_ROUTER stays callable as a compatibility shim until 1.4b completes.

Exports:
    ALL_TOOLS: Full tool list for execution dispatch.
    LEGACY_TOOLS / MODLIX_TOOLS / META_TOOLS / WORKSPACE_TOOLS / KB_APP_TOOLS:
        Per-surface lists, useful for tests and for the agent loop to scope
        which tools are eligible per turn.
    TOOL_ROUTER: Single meta-tool for LLM schema (tool-of-tools pattern).
"""

from __future__ import annotations

from app.core.tools.base import ToolDefinition, ToolParameter

from app.agents.appbuilder.tools.crud import CRUD_TOOLS
from app.agents.appbuilder.tools.version_tools import VERSION_TOOLS
from app.agents.appbuilder.tools.api_catalog_tools import API_CATALOG_TOOLS

# Modlix port surfaces — added incrementally as each category lands.
# Currently shipped: infra (env / cache / logs). The rest of the ~195 tools
# land in Phase 1.4b across follow-up sessions.
from app.agents.appbuilder.tools.modlix.infra import TOOLS as _MODLIX_INFRA_TOOLS
from app.agents.appbuilder.tools.modlix.components import TOOLS as _MODLIX_COMPONENT_TOOLS
from app.agents.appbuilder.tools.modlix.pages import TOOLS as _MODLIX_PAGE_TOOLS
from app.agents.appbuilder.tools.modlix.kirun import TOOLS as _MODLIX_KIRUN_TOOLS
from app.agents.appbuilder.tools.modlix.kirun_events import TOOLS as _MODLIX_KIRUN_EVENT_TOOLS
from app.agents.appbuilder.tools.modlix.schemas import TOOLS as _MODLIX_SCHEMA_TOOLS
from app.agents.appbuilder.tools.modlix.visuals import TOOLS as _MODLIX_VISUAL_TOOLS
from app.agents.appbuilder.tools.modlix.visuals_browser import TOOLS as _MODLIX_BROWSER_TOOLS
from app.agents.appbuilder.tools.modlix.image_ops import TOOLS as _MODLIX_IMAGE_OPS_TOOLS
from app.agents.appbuilder.tools.modlix.clone_ops import TOOLS as _MODLIX_CLONE_TOOLS
from app.agents.appbuilder.tools.modlix.security import TOOLS as _MODLIX_SECURITY_TOOLS
from app.agents.appbuilder.tools.modlix.app_admin import TOOLS as _MODLIX_APP_ADMIN_TOOLS
from app.agents.appbuilder.tools.modlix.messaging import TOOLS as _MODLIX_MESSAGING_TOOLS
from app.agents.appbuilder.tools.modlix.runtime import TOOLS as _MODLIX_RUNTIME_TOOLS
from app.agents.appbuilder.tools.meta_tools import META_TOOLS
from app.agents.appbuilder.tools.code_workspace import CODE_WORKSPACE_TOOLS as WORKSPACE_TOOLS
from app.agents.appbuilder.tools.kb_app import KB_APP_TOOLS
from app.agents.appbuilder.tools.platform_docs import PLATFORM_DOC_TOOLS

LEGACY_TOOLS: list[ToolDefinition] = CRUD_TOOLS + VERSION_TOOLS + API_CATALOG_TOOLS
# Vision routing — hide the Gemini-only `describe_image` tool when the
# AppBuilder runs on a vision-capable provider (Anthropic, OpenAI). The
# screenshot tools already short-circuit Gemini-describe for these providers;
# unregistering `describe_image` prevents the agent from reaching for it as
# a redundant secondary call.
def _filter_visual_tools(tools: list[ToolDefinition]) -> list[ToolDefinition]:
    try:
        from app.config import settings as _settings
        provider = (getattr(_settings, "APPBUILDER_PROVIDER", "") or "").lower()
    except Exception:  # noqa: BLE001
        provider = ""
    if provider in {"anthropic", "openai"}:
        return [t for t in tools if t.name != "describe_image"]
    return tools


MODLIX_TOOLS: list[ToolDefinition] = (
    list(_MODLIX_INFRA_TOOLS)
    + list(_MODLIX_COMPONENT_TOOLS)
    + list(_MODLIX_PAGE_TOOLS)
    + list(_MODLIX_KIRUN_TOOLS)
    + list(_MODLIX_KIRUN_EVENT_TOOLS)
    + list(_MODLIX_SCHEMA_TOOLS)
    + _filter_visual_tools(list(_MODLIX_VISUAL_TOOLS))
    + list(_MODLIX_BROWSER_TOOLS)
    + list(_MODLIX_IMAGE_OPS_TOOLS)
    + list(_MODLIX_CLONE_TOOLS)
    + list(_MODLIX_SECURITY_TOOLS)
    + list(_MODLIX_APP_ADMIN_TOOLS)
    + list(_MODLIX_MESSAGING_TOOLS)
    + list(_MODLIX_RUNTIME_TOOLS)
)  # Phase 1.4b modlix port complete

ALL_TOOLS: list[ToolDefinition] = (
    LEGACY_TOOLS
    + MODLIX_TOOLS
    + META_TOOLS
    + WORKSPACE_TOOLS
    + KB_APP_TOOLS
    + PLATFORM_DOC_TOOLS
)

# ── Tool-of-tools router (DEPRECATED — retired from AppBuilderAgent) ─────────
#
# Phase 3 of the CFA rewrite switched the AppBuilder agent to the deferred-
# schema surface (`defer_schemas=True` in agent.py), so the LLM now sees
# every advertised tool by name with empty params and pulls full schemas on
# demand via `get_tool_schema`. The system prompt's tool catalog (see
# TOOL_GROUPS_SUMMARY in app.agents.appbuilder.context) lists the 200
# advertised tools by group.
#
# TOOL_ROUTER is kept exported here for any external caller that still
# constructs a router-mode agent (e.g. a future sub-agent that wants the
# legacy 10-verb interface). The 10 legacy CRUD verbs + version_api +
# lookup_api remain in ALL_TOOLS as callable fallbacks but are intentionally
# hidden from the LLM's catalog (see _INTENTIONALLY_HIDDEN in context.py).

_ROUTER_DESCRIPTION = """\
Execute an appbuilder tool.  Pass the tool name and its parameters.

Tools:
- list(object_type, app_code?) — list entities
- create(object_type, name, message, ...) — create entity
- read(object_type, id|name, include?, component_key?, ...) — read entity
- update(object_type, message, id|page_name, operations?, ...) — update entity
- delete(object_type, id|app_code) — delete entity
- copy(object_type, source_app_code, source_name, target_app_code, ...) — copy definitions
- list_versions(object_id, entity_type) — version history
- read_version(version_id, entity_type) — read a version snapshot
- rollback_version(version_id, entity_type, message) — rollback to version
- lookup_api(service, entity?) — API reference lookup

See the system prompt for detailed parameter docs per tool."""

TOOL_ROUTER = ToolDefinition(
    name="execute",
    display_name="Execute Tool",
    description=_ROUTER_DESCRIPTION,
    parameters=[
        ToolParameter(
            name="tool",
            type="string",
            description="Tool name to execute.",
            required=True,
            enum=[t.name for t in ALL_TOOLS],
        ),
        ToolParameter(
            name="params",
            type="object",
            description="Tool parameters as key-value pairs.",
            required=True,
        ),
    ],
    execute=None,  # Dispatch handled by the agent loop
)
