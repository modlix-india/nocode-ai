"""create — generic create tool definition."""

from __future__ import annotations

from typing import Any

from app.core.tools.base import ToolDefinition, ToolResult, ToolParameter
from app.agents.appbuilder.tools.crud._registry import OBJECT_TYPES, OBJECT_TYPE_ENUM
from app.agents.appbuilder.tools.crud._handlers import generic_create


async def _create_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    object_type = params.get("object_type")
    if not object_type:
        return ToolResult(
            success=False,
            error=f"`object_type` is required. One of: {OBJECT_TYPE_ENUM}.",
        )
    config = OBJECT_TYPES.get(object_type)
    if not config:
        return ToolResult(success=False, error=f"Unknown object_type: {object_type}")
    return await generic_create(config, params, context)


create_tool = ToolDefinition(
    name="create",
    display_name="Create",
    # v8 Plan B WS4 · declarative only. This tool already elicits the user via
    # request_confirmation (blocking, in-tool) — see AppBuilderAgent.CONFIRMATION_TOOLS.
    # Marking it keeps the registry honest; zero runtime change (the run-loop
    # break fires only for elicit_mode="deferred").
    kind="elicitation",
    elicit_mode="blocking",
    description=(
        "Create a new object. Returns the created object's ID.\n\n"
        "Type-specific params:\n"
        "- page: name=page name (letters only). Creates with root Grid layout.\n"
        "- application: name=display name, app_code=unique code (letters only, required), "
        "app_type='APP'|'SITE'.\n"
        "- theme: variables={breakpoint: {key: value}} (e.g. {\"ALL\": {\"primaryColor\": \"#3B82F6\"}}). "
        "MUST set confirmed=true after user approves.\n"
        "- function: definition=KIRun function def (required), namespace=dot notation namespace.\n"
        "- schema: definition=schema body (required).\n"
        "- style/connection/workflow/template/uripath: definition=entity definition.\n\n"
        "Common params: name (required), message (required), title, description, app_code.\n"
        "Names must be letters only (a-z, A-Z) except for application display name."
    ),
    parameters=[
        ToolParameter(
            name="object_type", type="string",
            description="Type of object to create.",
            enum=OBJECT_TYPE_ENUM,
        ),
        ToolParameter(name="name", type="string", description="Object name (letters only for most types). For 'application', this is the display name."),
        ToolParameter(name="message", type="string", description="Commit message (10-15 words) describing what was created."),
        ToolParameter(name="app_code", type="string", required=False, description="Application code. For 'application', the unique app code to create (letters only, required)."),
        ToolParameter(name="definition", type="object", required=False, description="Object definition. Required for function, schema. Not used for theme (use variables) or application."),
        ToolParameter(name="title", type="string", required=False, description="Human-readable title."),
        ToolParameter(name="description", type="string", required=False, description="Object description."),
        ToolParameter(name="namespace", type="string", required=False, description="Function namespace. Only for object_type='function'."),
        ToolParameter(name="variables", type="object", required=False, description="Theme variables by breakpoint. Only for object_type='theme'. E.g. {\"ALL\": {\"primaryColor\": \"#3B82F6\"}}."),
        ToolParameter(name="confirmed", type="boolean", required=False, description="Required for theme creation. Must be true after user approves the app-wide change."),
        ToolParameter(name="app_type", type="string", required=False, description="Only for object_type='application'. 'APP' (authenticated) or 'SITE' (public). Defaults to 'APP'."),
    ],
    execute=_create_execute,
)
