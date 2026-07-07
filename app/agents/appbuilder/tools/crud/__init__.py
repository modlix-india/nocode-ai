"""Generic CRUD tools package — 5 tools for all entity types.

Exports CRUD_TOOLS for registration in the tool registry.
"""

from __future__ import annotations

from app.core.tools.base import ToolDefinition

from app.agents.appbuilder.tools.crud.list_handler import list_tool
from app.agents.appbuilder.tools.crud.create_handler import create_tool
from app.agents.appbuilder.tools.crud.read_handler import read_tool
from app.agents.appbuilder.tools.crud.update_handler import update_tool
from app.agents.appbuilder.tools.crud.delete_handler import delete_tool
from app.agents.appbuilder.tools.crud.copy_handler import copy_tool

CRUD_TOOLS: list[ToolDefinition] = [
    list_tool,
    create_tool,
    read_tool,
    update_tool,
    delete_tool,
    copy_tool,
]
