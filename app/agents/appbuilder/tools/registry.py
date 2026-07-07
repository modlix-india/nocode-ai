"""Tool registry — imports and exports ALL appbuilder tools.

Used by AppBuilderAgent.__init__() to register all available tools.

Total: 10 tools (6 CRUD + 3 version + 1 API catalog).

Exports:
    ALL_TOOLS: Full tool list for execution dispatch.
    TOOL_ROUTER: Single meta-tool for LLM schema (tool-of-tools pattern).
"""

from __future__ import annotations

from app.core.tools.base import ToolDefinition, ToolParameter

from app.agents.appbuilder.tools.crud import CRUD_TOOLS
from app.agents.appbuilder.tools.version_tools import VERSION_TOOLS
from app.agents.appbuilder.tools.api_catalog_tools import API_CATALOG_TOOLS

ALL_TOOLS: list[ToolDefinition] = CRUD_TOOLS + VERSION_TOOLS + API_CATALOG_TOOLS

# ── Tool-of-tools router ────────────────────────────────────────
#
# Instead of sending all 10 tool schemas (~1300 tokens) to the LLM,
# send one lightweight "execute" tool (~150 tokens).  The agent
# unwraps execute(tool="read", params={...}) into the real tool call.

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
