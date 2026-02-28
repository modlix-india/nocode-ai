"""Tool registry — imports and exports ALL appbuilder tools.

Used by AppBuilderAgent.__init__() to register all available tools.
"""

from __future__ import annotations

from app.core.tools.base import ToolDefinition

from app.agents.appbuilder.tools.page_tools import PAGE_TOOLS
from app.agents.appbuilder.tools.component_tools import COMPONENT_TOOLS
from app.agents.appbuilder.tools.event_tools import EVENT_TOOLS
from app.agents.appbuilder.tools.application_tools import APPLICATION_TOOLS
from app.agents.appbuilder.tools.style_tools import STYLE_TOOLS
from app.agents.appbuilder.tools.function_tools import FUNCTION_TOOLS, SCHEMA_TOOLS
from app.agents.appbuilder.tools.entity_tools import ENTITY_TOOLS

ALL_TOOLS: list[ToolDefinition] = (
    PAGE_TOOLS
    + COMPONENT_TOOLS
    + EVENT_TOOLS
    + APPLICATION_TOOLS
    + STYLE_TOOLS
    + FUNCTION_TOOLS
    + SCHEMA_TOOLS
    + ENTITY_TOOLS
)
