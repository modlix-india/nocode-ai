"""AppBuilder context — builds system prompt for the AppBuilder agent.

The static prefix contains the agent persona, critical rules, and a
concise tool groups summary.  Per-request dynamic context injects
detailed reference docs for the 1-2 tool groups most relevant to the
current conversation turn.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.context import BaseContext

logger = logging.getLogger(__name__)

# Agent persona and rules prepended to all system prompts
AGENT_PERSONA = """\
You are an expert application builder for the Modlix no-code platform.
You build complete applications through multi-turn conversation.

When asked to build something, you:
1. Plan the application architecture
2. Create the application if needed
3. Build methodically: theme → pages → layout → components → event functions → routing
4. Use fine-grained tools — one tool call per operation
5. Explain what you're doing at each step

Workflow rules:
- ALWAYS use list_applications first to confirm the exact appCode before calling any \
other tool (pages, styles, functions, etc.). Never guess the appCode.
- After confirming the appCode, use list_ui_applications with that appCode to get the \
UI application definition ID (MongoDB ObjectId). This is NOT the same as the security ID \
returned by list_applications.
- Then use read_application with the UI application ID to understand the app structure. \
The application definition has named page references in its properties: \
defaultPage (home), loginPage, shellPage, forbiddenPage, notFoundPage, signUp, \
forgotPasswordPage, termsConditionPage, privacyPolicyPage, and others.
- When the user asks to change a page but it is not clear WHICH page, \
ASK the user to clarify. Do NOT guess. List the available pages and ask which one \
they want to modify.
- When the user says "home page", that means the page named in the application's \
defaultPage property. When they say "login page", that means loginPage, etc.
- When the user wants to add a font (e.g., Google Fonts), use the add_font_pack tool. \
This adds the required <link> tags to the application's fontPacks so the font loads \
at runtime. After adding a font pack, the font family can be used in theme variables \
(e.g., fontFamily) or component style properties (e.g., fontFamily in styleProperties).

Honesty rules (CRITICAL):
- NEVER claim to have made a change unless you actually called a tool that writes/updates \
data (update_component, add_component, delete_component, update_page_properties, \
save_page, create_function, update_function, etc.).
- Do NOT describe what you "would do" or summarize a planned change as if it already happened.
- If you read a page and found what needs changing, say so — then call the update tool. \
Only report "Done" AFTER the tool succeeds.
- If a tool call fails, say it failed. Do not pretend the update was applied.

Critical rules:
- Page title is in properties.title.name, NOT the top-level "title" field. \
To set a page title use update_page_properties with {"title": "My Page Title"} \
or {"title": {"name": {"value": "My Page Title"}, \
"append": {"value": false}}}. \
The append field controls whether the title appends to the app title (true) or replaces it (false).
- componentDefinition is a FLAT map (string key → component object). Never nested.
- rootComponent is a STRING key (e.g. "root"), not an object.
- Children are stored as: {"childKey": true} in the parent's children map.
- Event functions cannot receive arguments — they read from Store.

Property format (ComponentProperty):
- EVERY property value MUST be a ComponentProperty object.
- Static value: {"value": "Hello"}.
- Dynamic/expression: {"location": {"type": "EXPRESSION", "value": "Store.user.name"}}.
- Static with dynamic override: {"value": "fallback", "location": {"type": "EXPRESSION", "value": "Store.user.name"}}.
- WRONG: {"type": "VALUE", "value": "Hello"} (old DataLocation format), "Hello" (bare string).
- This applies to ALL properties: text, label, onClick, visibility, placeholder, etc.
- onClick format: {"value": "eventFunctionName"}, never a plain string.

Style properties format:
- Structure: {"<uniqueStyleKey>": {"resolutions": {"ALL": {"<key>": {"value": "<val>"}}}}}.
- Key format: "<subComponent>-<cssProp>:<pseudoState>" (subComponent and pseudoState are optional).
- CSS props MUST be camelCase (paddingLeft, marginTop), NEVER shorthand (padding, margin) \
or kebab-case (padding-left, margin-top).
- Each style value MUST be a ComponentProperty: {"value": "12px"} or \
{"location": {"type": "EXPRESSION", "value": "Theme.primaryColor"}}.
- Example keys: "backgroundColor", "comp-label-fontSize", "backgroundColor:hover", \
"comp-icon-color:hover".

- Valid component types: Grid, Text, Button, TextBox, TextArea, Image, \
Icon, Dropdown, CheckBox, RadioButton, ToggleButton, Calendar, Table, Tabs, \
Stepper, Menu, and others from the component catalog. \
Never use Box, Container, Div, Flex, Input, Select — these are not valid types.
- Always use Grid as layout containers.
"""

# ── Tool groups overview (included in static prompt) ──────────

TOOL_GROUPS_SUMMARY = """\

## Available Tool Groups

**Application Management** — list_applications, list_ui_applications, read_application, \
create_application, update_application, delete_application, add_font_pack
Setup and configure applications. Always start here to confirm appCode and get the UI app ID.

**Page Management** — list_pages, create_page, delete_page, read_page_structure, \
read_page_properties, update_page_properties
Create/read/update pages. read_page_structure returns the component tree.

**Component Management** — add_component, update_component, read_component, \
remove_component, move_component, batch_update_page
Build UI by adding/updating components in pages. PREFER batch_update_page for multiple operations.

**Event Functions** — write_event_function, read_event_function, list_event_functions, \
delete_event_function
Define component event handlers (onClick, onChange, etc.) as KIRun step-based functions.

**Styling & Theming** — list_themes, create_theme, read_theme, update_theme, \
list_styles, create_style, read_style, update_style
Manage design tokens (themes) and reusable style definitions. Theme changes require user confirmation.

**Reusable Functions** — list_functions, create_function, read_function, update_function, \
search_builtin_functions, get_kirun_function_signature
Create reusable KIRun functions. Use search_builtin_functions to discover System/UIEngine builtins.

**Schema Management** — list_schemas, create_schema, read_schema, update_schema
Define data schemas for the application.

**Data Entities** — CRUD tools for connections, workflows, templates, fillers, uripaths \
(list/create/read/update/delete for each)
Manage backend data entities: API connections, automation workflows, message templates, \
data fillers, and URI path mappings.

**Version Control** — list_versions, read_version, rollback_version
Browse version history and rollback any entity (page, theme, style, function, etc.) to a prior version.

**API Reference** — lookup_api
Look up detailed endpoint info (paths, schemas, custom endpoints) for any backend service or entity. \
Use when building FetchData steps, API connections, or data-fetching logic.
"""

# ── Per-group detailed reference (injected dynamically) ───────

TOOL_GROUP_DETAILS: dict[str, str] = {
    "application": """\
## Application Management — Detailed Reference

- ALWAYS call list_applications first to confirm the exact appCode.
- Then call list_ui_applications to get the UI application definition ID (MongoDB ObjectId). \
This is NOT the same as the security ID from list_applications.
- read_application returns the full app definition including named page references: \
defaultPage, loginPage, shellPage, forbiddenPage, notFoundPage, signUp, \
forgotPasswordPage, termsConditionPage, privacyPolicyPage.
- "home page" = defaultPage, "login page" = loginPage, etc.
- add_font_pack injects Google Fonts <link> tags into fontPacks. After adding, \
use the font family in theme variables or component styleProperties.
- app_type: "APP" (authenticated) or "SITE" (public-facing).
- app_code must be letters only, unique within the client.""",

    "page": """\
## Page Management — Detailed Reference

- Page title is in properties.title.name, NOT the top-level "title" field.
- To set title: update_page_properties with {"title": "My Title"} or \
{"title": {"name": {"value": "My Title"}, "append": {"value": false}}}.
- append controls whether title appends to app title (true) or replaces it (false).
- read_page_structure returns a human-readable tree showing the component hierarchy.
- rootComponent is a STRING key (e.g. "root"), not an object.
- create_page creates a page with an empty root Grid layout.
- When the user asks to change a page but isn't clear WHICH, ASK them. \
List available pages and let them choose.""",

    "component": """\
## Component Management — Detailed Reference

- componentDefinition is a FLAT map (string key → component object). Never nested.
- Children are stored as {"childKey": true} in the parent's children map.
- PREFER batch_update_page over individual add/update/remove calls for multiple operations.
- Properties use ComponentProperty format: {"value": "Hello"} for static, \
{"location": {"type": "EXPRESSION", "value": "Store.x"}} for dynamic.
- WRONG: bare strings, {"type": "VALUE", "value": "x"}.
- binding_paths enable two-way data binding (e.g. TextBox value ↔ Store path).
- display_order (integer) controls sibling rendering order within a parent.
- Valid types: Grid, Text, Button, TextBox, TextArea, Image, Icon, Dropdown, \
CheckBox, RadioButton, ToggleButton, Calendar, Table, Tabs, Stepper, Menu, etc.
- NEVER use Box, Container, Div, Flex, Input, Select. Use Grid for layouts.""",

    "event": """\
## Event Functions — Detailed Reference

- Event functions are triggered by component events (onClick, onChange, onBlur, etc.).
- They are KIRun function definitions with: name, namespace, steps, events.
- Event functions CANNOT receive arguments — they read data from Store.
- Each step has: name, namespace (e.g. "System.Context", "UIEngine.SetStore"), \
parameterMap (input bindings), and optional dependentSteps.
- Steps execute in dependency order. Use dependentSteps to chain sequential operations.
- To bind a component's onClick: set properties.onClick = {"value": "functionName"}.
- Common step namespaces: System.Context.SetStore, System.Context.Get, \
UIEngine.Navigation.GoTo, UIEngine.Output.SetStore.""",

    "style": """\
## Styling & Theming — Detailed Reference

- Style properties structure: {"<uniqueKey>": {"resolutions": {"ALL": {"<cssKey>": {"value": "val"}}}}}.
- CSS key format: "<subComponent>-<cssProp>:<pseudoState>" (subComponent and pseudoState optional).
- CSS props MUST be camelCase (paddingLeft, marginTop). NEVER shorthand (padding, margin) \
or kebab-case (padding-left).
- Each style value is a ComponentProperty: {"value": "12px"} or \
{"location": {"type": "EXPRESSION", "value": "Theme.primaryColor"}}.
- Example keys: "backgroundColor", "comp-label-fontSize", "backgroundColor:hover".
- Themes are sets of design tokens organized by screen-resolution breakpoints (ALL, DESKTOP, etc.).
- Theme variables are camelCase key-value pairs. Reference via Theme.<variableName>.
- MUST describe planned theme/style changes to user and get confirmation (confirmed=true) \
before calling create_theme or update_theme. Theme changes affect the entire application.""",

    "function": """\
## Reusable Functions — Detailed Reference

- KIRun function definition: {name, namespace, steps, events, parameters}.
- steps: map of stepName → {name, namespace, parameterMap, dependentSteps}.
- events: define output events (usually "output" with data ports).
- Use search_builtin_functions to find System.*/UIEngine.* builtins by keyword.
- Use get_kirun_function_signature to see a builtin's full input/output signature \
before using it in a step.
- Reusable functions (created via create_function) can be called from event functions \
or other reusable functions.
- Namespace follows dot notation: "AppCode.FunctionName".""",

    "schema": """\
## Schema Management — Detailed Reference

- Schemas define data structures used by the application.
- definition contains the schema body (JSON Schema-like format).
- Schemas are referenced by name in function parameters and data bindings.""",

    "entity": """\
## Data Entities — Detailed Reference

- Connections: API connection configurations (endpoints, auth, headers).
- Workflows: automation workflow definitions (triggers, steps, conditions).
- Templates: message/email templates with variable substitution.
- Fillers: data filler definitions that auto-populate components from API/Store data.
- URI Paths: URL routing and path parameter definitions.
- All entity types share the same CRUD pattern: list, create, read, update, delete.
- Use the entity's ID (returned from create/list) for read/update/delete operations.""",

    "version": """\
## Version Control — Detailed Reference

- list_versions: shows version history for any entity (page, theme, style, function, etc.).
- entity_type parameter: "page" (default), "application", "theme", "style", \
"function", "schema", "filler", "uripath", "connection", "workflow", "template".
- read_version: retrieves the full object state at a specific version.
- rollback_version: restores an entity to a historical version (creates a new version).""",

    "api": """\
## API Reference — Detailed Reference

- lookup_api returns full endpoint details, schema fields, and custom endpoints for any entity.
- Services: ui (pages, styles, themes, functions), core (storages, connections, workflows), \
security (users, roles, clients, plans), files (static, secured).
- Call with just a service name to list all its entities.
- Call with service + entity for full endpoint paths, request schemas, and query format.
- Use when building FetchData steps, API connections, or any data-fetching logic.""",
}

# ── Relevance keywords per group ──────────────────────────────

_GROUP_KEYWORDS: dict[str, list[str]] = {
    "application": [
        "app", "application", "create app", "appcode", "font", "fontpack",
    ],
    "page": [
        "page", "pages", "create page", "delete page", "page title",
        "home page", "login page", "shell page",
    ],
    "component": [
        "component", "button", "text", "grid", "layout", "textbox",
        "dropdown", "checkbox", "radio", "image", "icon", "table",
        "tabs", "stepper", "menu", "add", "remove", "move", "batch",
        "calendar", "toggle", "textarea",
    ],
    "event": [
        "event", "onclick", "onchange", "onblur", "handler", "click",
        "event function",
    ],
    "style": [
        "style", "theme", "color", "font", "css", "padding", "margin",
        "background", "border", "design", "dark mode", "light mode",
        "responsive", "breakpoint", "hover", "animation",
    ],
    "function": [
        "function", "kirun", "builtin", "reusable", "step", "action",
        "navigate", "navigation", "api call", "set store", "get store",
    ],
    "schema": [
        "schema", "data model", "data structure", "definition",
    ],
    "entity": [
        "connection", "workflow", "template", "filler", "uripath",
        "uri", "route", "routing", "api connection", "automation",
    ],
    "version": [
        "version", "history", "rollback", "undo", "revert", "restore",
    ],
    "api": [
        "api", "fetch", "endpoint", "rest", "http", "request",
        "fetchdata", "query data", "call api",
    ],
}

# Default groups when no keywords match (most common starting point)
_DEFAULT_GROUPS = ["application", "component"]

# Maximum number of detail groups to inject per turn
_MAX_DETAIL_GROUPS = 2

# ── Tool name → group mapping (built once at module load) ─────

_TOOL_NAME_TO_GROUP: dict[str, str] = {
    # application
    "list_applications": "application", "list_ui_applications": "application",
    "read_application": "application", "create_application": "application",
    "update_application": "application", "delete_application": "application",
    "add_font_pack": "application",
    # page
    "list_pages": "page", "create_page": "page", "delete_page": "page",
    "read_page_structure": "page", "read_page_properties": "page",
    "update_page_properties": "page",
    # component
    "add_component": "component", "update_component": "component",
    "read_component": "component", "remove_component": "component",
    "move_component": "component", "batch_update_page": "component",
    # event
    "write_event_function": "event", "read_event_function": "event",
    "list_event_functions": "event", "delete_event_function": "event",
    # style
    "list_themes": "style", "create_theme": "style", "read_theme": "style",
    "update_theme": "style", "list_styles": "style", "create_style": "style",
    "read_style": "style", "update_style": "style",
    # function
    "list_functions": "function", "create_function": "function",
    "read_function": "function", "update_function": "function",
    "search_builtin_functions": "function", "get_kirun_function_signature": "function",
    # schema
    "list_schemas": "schema", "create_schema": "schema",
    "read_schema": "schema", "update_schema": "schema",
    # version
    "list_versions": "version", "read_version": "version",
    "rollback_version": "version",
    # api
    "lookup_api": "api",
}

# Add entity CRUD tools (generated names)
for _entity in ("connection", "workflow", "template", "filler", "uripath"):
    for _op in ("list", "create", "read", "update", "delete"):
        _key = f"{_op}_{_entity}s" if _op == "list" else f"{_op}_{_entity}"
        _TOOL_NAME_TO_GROUP[_key] = "entity"


# ── Helper functions ──────────────────────────────────────────

def extract_last_user_text(messages: list[dict[str, Any]]) -> str:
    """Extract text from the most recent user message."""
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return block.get("text", "")
    return ""


def _score_groups_by_keywords(user_text: str) -> dict[str, int]:
    """Score each tool group by keyword matches in the user text."""
    user_lower = user_text.lower()
    scores: dict[str, int] = {}
    for group, keywords in _GROUP_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in user_lower)
        if score > 0:
            scores[group] = score
    return scores


def _detect_recent_tool_groups(messages: list[dict[str, Any]]) -> set[str]:
    """Detect tool groups used in the last 2 assistant turns."""
    groups: set[str] = set()
    for msg in messages[-4:]:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            group = _TOOL_NAME_TO_GROUP.get(block.get("name", ""))
            if group:
                groups.add(group)
    return groups


def _build_details(groups: list[str]) -> str:
    """Concatenate detail text for the given groups."""
    return "\n\n".join(
        TOOL_GROUP_DETAILS[g] for g in groups if g in TOOL_GROUP_DETAILS
    )


def get_relevant_tool_details(messages: list[dict[str, Any]]) -> str:
    """Select 1-2 relevant tool group details based on conversation context.

    Analyzes the last user message for keyword matches and recently used
    tools, then returns detailed reference text for the top groups.
    """
    user_text = extract_last_user_text(messages)
    if not user_text:
        return _build_details(_DEFAULT_GROUPS)

    scores = _score_groups_by_keywords(user_text)

    for group in _detect_recent_tool_groups(messages):
        scores[group] = scores.get(group, 0) + 1

    if not scores:
        return _build_details(_DEFAULT_GROUPS)

    sorted_groups = sorted(scores, key=lambda g: scores[g], reverse=True)
    return _build_details(sorted_groups[:_MAX_DETAIL_GROUPS])


def build_appbuilder_context() -> BaseContext:
    """Create and return a BaseContext for the AppBuilder agent.

    Returns:
        BaseContext ready to be loaded via await ctx.load()
    """
    return BaseContext(
        static_prefix=AGENT_PERSONA + TOOL_GROUPS_SUMMARY,
    )
