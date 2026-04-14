"""Tool registry — imports and exports ALL appbuilder tools.

Used by AppBuilderAgent to register tools.  Tools are split into two tiers:

  CORE_TOOLS:     Always loaded in the initial prompt (~6 tools).
  DEFERRED_TOOLS: Discovered on-demand via ToolSearchTool.

The old TOOL_ROUTER (tool-of-tools meta-tool) is removed — deferred
loading achieves better token savings while giving the LLM native
tool schemas.

Exports:
    CORE_TOOLS: Tools always sent to the LLM.
    DEFERRED_TOOLS: Tools only sent after discovery via tool_search.
    ALL_TOOLS: Full list for execution dispatch (core + deferred).
"""

from __future__ import annotations

from app.core.tools.base import ToolDefinition

# ── Core tools (always loaded) ───────────────────────────────────
# These cover the 80% use case: discover app structure, list/read/
# create/update entities, and discover more tools.

from app.agents.appbuilder.tools.crud.list_handler import list_tool
from app.agents.appbuilder.tools.crud.create_handler import create_tool
from app.agents.appbuilder.tools.crud.read_handler import read_tool
from app.agents.appbuilder.tools.crud.update_handler import update_tool
from app.agents.appbuilder.tools.tool_search import TOOL_SEARCH

# app_index will be added here once page_tools.py is built.
# For now core tools are the existing CRUD + tool_search.

CORE_TOOLS: list[ToolDefinition] = [
    list_tool,
    create_tool,
    read_tool,
    update_tool,
    TOOL_SEARCH,
]


# ── Deferred tools (discovered via ToolSearchTool) ───────────────
# These tools are NOT included in the initial prompt.  The LLM
# discovers them by calling tool_search with relevant keywords.

from app.agents.appbuilder.tools.crud.delete_handler import delete_tool
from app.agents.appbuilder.tools.crud.copy_handler import copy_tool
from app.agents.appbuilder.tools.version_tools import VERSION_TOOLS
from app.agents.appbuilder.tools.api_catalog_tools import API_CATALOG_TOOLS
from app.agents.appbuilder.tools.result_store import READ_RESULT_TOOL
from app.agents.appbuilder.tools.page_tools import PAGE_TOOLS
from app.agents.appbuilder.tools.theme_style_tools import THEME_STYLE_TOOLS
from app.agents.appbuilder.tools.function_tools import FUNCTION_TOOLS
from app.agents.appbuilder.tools.data_tools import DATA_TOOLS
from app.agents.appbuilder.tools.app_config_tools import APP_CONFIG_TOOLS
from app.agents.appbuilder.tools.remote_repo import REMOTE_REPO_TOOLS
from app.agents.appbuilder.tools.planning import PLANNING_TOOLS
from app.agents.appbuilder.tools.orchestration import ORCHESTRATION_TOOLS
from app.agents.appbuilder.tools.clone_tool import CLONE_WEBSITE
from app.agents.appbuilder.tools.screenshot_tool import BUILD_PAGE_FROM_SCREENSHOT

# Mark existing tools as deferred
delete_tool.is_deferred = True
delete_tool.search_hint = "delete remove page theme function application entity"

copy_tool.is_deferred = True
copy_tool.search_hint = "copy duplicate clone entity across apps"

for vt in VERSION_TOOLS:
    vt.is_deferred = True
    if "list" in vt.name:
        vt.search_hint = "version history changelog audit trail"
    elif "read" in vt.name:
        vt.search_hint = "read historical version snapshot previous"
    elif "rollback" in vt.name:
        vt.search_hint = "restore rollback revert undo version"

for at in API_CATALOG_TOOLS:
    at.is_deferred = True
    at.search_hint = "API endpoint documentation REST reference"

DEFERRED_TOOLS: list[ToolDefinition] = [
    delete_tool,
    copy_tool,
    *VERSION_TOOLS,
    *API_CATALOG_TOOLS,
    READ_RESULT_TOOL,
    *PAGE_TOOLS,
    *THEME_STYLE_TOOLS,
    *FUNCTION_TOOLS,
    *DATA_TOOLS,
    *APP_CONFIG_TOOLS,
    *REMOTE_REPO_TOOLS,
    *PLANNING_TOOLS,
    *ORCHESTRATION_TOOLS,
    CLONE_WEBSITE,
    BUILD_PAGE_FROM_SCREENSHOT,
]


# ── Combined list for dispatch ───────────────────────────────────

ALL_TOOLS: list[ToolDefinition] = CORE_TOOLS + DEFERRED_TOOLS
