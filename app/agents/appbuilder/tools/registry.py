"""Tool registry — imports and exports ALL appbuilder tools.

Used by AppBuilderAgent.__init__() to register all available tools.

Total: 9 tools (5 CRUD + 3 version + 1 API catalog).
"""

from __future__ import annotations

from app.core.tools.base import ToolDefinition

from app.agents.appbuilder.tools.crud import CRUD_TOOLS
from app.agents.appbuilder.tools.version_tools import VERSION_TOOLS
from app.agents.appbuilder.tools.api_catalog_tools import API_CATALOG_TOOLS

ALL_TOOLS: list[ToolDefinition] = CRUD_TOOLS + VERSION_TOOLS + API_CATALOG_TOOLS
