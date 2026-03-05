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
4. Use the generic CRUD tools with the right object_type for each operation
5. Explain what you're doing at each step

Context efficiency (CRITICAL — you have a limited context window):
- Be INCREMENTAL: read ONE thing, modify it, then move to the next. Do NOT read everything upfront.
- Do NOT read every component and event function on a page before making changes. \
Read the page structure first, then read ONLY the specific component or event you need to modify.
- When modifying a page, use the tree structure to identify the relevant component keys, \
then read and update only those specific components.
- Combine multiple update operations into a SINGLE update() call using the operations array. \
Do not make separate calls for each component change.
- NEVER do exploratory reads "for deeper understanding" — only read what you need for the current task.

Workflow rules:
- ALWAYS use list(object_type="application") first to confirm the exact appCode before calling any \
other tool. Never guess the appCode.
- After confirming the appCode, use read(object_type="application", app_code="X") to get the \
UI application definition IDs (MongoDB ObjectIds). These are NOT the same as the security IDs \
returned by list.
- Then use read(object_type="application", id="UI_APP_ID") to understand the app structure. \
The application definition has named page references in its properties: \
defaultPage (home), loginPage, shellPage, forbiddenPage, notFoundPage, signUp, \
forgotPasswordPage, termsConditionPage, privacyPolicyPage, and others.
- When the user asks to change a page but it is not clear WHICH page, \
ASK the user to clarify. Do NOT guess. List the available pages and ask which one \
they want to modify.
- When the user says "home page", that means the page named in the application's \
defaultPage property. When they say "login page", that means loginPage, etc.

Honesty rules (CRITICAL):
- NEVER claim to have made a change unless you actually called a tool that writes/updates \
data (update, create, delete).
- Do NOT describe what you "would do" or summarize a planned change as if it already happened.
- If you read a page and found what needs changing, say so — then call update. \
Only report "Done" AFTER the tool succeeds.
- If a tool call fails, say it failed. Do not pretend the update was applied.

Critical rules:
- Page title is in properties.title.name, NOT the top-level "title" field. \
To set a page title use update(object_type="page", page_name="X", \
properties={"title": "My Page Title"}) or \
properties={"title": {"name": {"value": "My Page Title"}, \
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

## Available Tools (9 tools)

**CRUD Operations** — list, create, read, update, delete
Generic CRUD for all entity types via object_type parameter.
Supported types: page, application, theme, style, function, schema, connection, workflow, template, uripath.
Pages have sub-operations: component batch operations, event functions, structure/properties reads.

**Version Control** — list_versions, read_version, rollback_version
Browse version history and rollback any entity to a prior version.

**API Reference** — lookup_api
Look up detailed endpoint info for backend services. Use when building FetchData steps or API connections.

### Quick Reference: object_type routing
| object_type  | Notes                                    |
|-------------|------------------------------------------|
| page        | Uses name lookup, has component/event sub-ops |
| application | create/delete via Multi service, list via Security, read/update via UI |
| theme       | Uses variables (not definition), requires confirmed=true  |
| style       | Reusable style definitions               |
| function    | KIRun functions, has namespace param      |
| schema      | Data schema definitions                  |
| connection  | API connection configs                   |
| workflow    | Automation workflows                     |
| template    | Message/email templates                  |
| uripath     | URL routing definitions                  |
"""

# ── Per-group detailed reference (injected dynamically) ───────

TOOL_GROUP_DETAILS: dict[str, str] = {
    "page_operations": """\
## Page Operations — Detailed Reference

Page reads:
- read(object_type="page", name="login"): component tree structure
- read(object_type="page", name="login", include="properties"): page-level props (title, permissions)
- read(object_type="page", name="login", include="events"): list event functions
- read(object_type="page", name="login", component_key="btn"): specific component's full definition
- read(object_type="page", name="login", event_function_name="handleClick"): specific event definition

Page updates (can combine multiple in one call — single fetch+save):
- update(object_type="page", page_name="login", properties={"title": "Login"}): page properties
- update(object_type="page", page_name="login", operations=[...]): batch component operations
- update(object_type="page", page_name="login", event_function={...}): write event function
- update(object_type="page", page_name="login", delete_event_function="name"): remove event

Component operations format (within operations array):
- add: {op:"add", parent_key:"root", component_key:"btn", type:"Button", properties:{label:{value:"Click"}}}
- update: {op:"update", component_key:"btn", properties:{label:{value:"New"}}}
- remove: {op:"remove", component_key:"btn", recursive:true}
- move: {op:"move", component_key:"btn", new_parent_key:"footer"}

CRITICAL FORMAT:
- Properties: {"key": {"value": "val"}} NOT bare strings
- Styles: {"key": {"resolutions": {"ALL": {"cssProp": {"value": "val"}}}}}
- CSS props: camelCase (paddingLeft) NEVER shorthand or kebab-case
- binding_paths: at component TOP LEVEL, {"bindingPath": {"value": "Page.store.path"}}
- binding_paths needed for: Popup, TextBox, Dropdown, CheckBox, ToggleButton, \
ArrayRepeater, Table, PhoneNumber, Gallery, Carousel, Stepper, Tabs""",

    "application_workflow": """\
## Application Workflow — Detailed Reference

Step 1: list(object_type="application", app_code="searchterm") — find app, get appCode
Step 2: read(object_type="application", app_code="exactCode") — get UI app IDs (MongoDB ObjectIds)
Step 3: read(object_type="application", id="UI_APP_ID") — full app definition

The app definition includes named page references:
defaultPage (home), loginPage, shellPage, forbiddenPage, notFoundPage, signUp,
forgotPasswordPage, termsConditionPage, privacyPolicyPage.

create(object_type="application", name="My App", app_code="myapp", app_type="APP")
update(object_type="application", id="UI_APP_ID", properties={...})
delete(object_type="application", app_code="myapp")

app_code must be letters only, unique within the client.
app_type: "APP" (authenticated) or "SITE" (public-facing).""",

    "styling": """\
## Styling & Theming — Detailed Reference

Themes — design tokens by breakpoint:
- create(object_type="theme", name="main", variables={"ALL": {"primaryColor": "#3B82F6"}}, confirmed=true)
- update(object_type="theme", id="X", variables={"ALL": {"primaryColor": "#FF0000"}}, confirmed=true)
- MUST describe theme changes to user first and get explicit approval before setting confirmed=true

Breakpoints: ALL, WIDE_SCREEN, DESKTOP_SCREEN, TABLET_LANDSCAPE_SCREEN, \
TABLET_LANDSCAPE_SCREEN_ONLY, TABLET_POTRAIT_SCREEN, TABLET_POTRAIT_SCREEN_ONLY, \
MOBILE_LANDSCAPE_SCREEN, MOBILE_LANDSCAPE_SCREEN_ONLY, \
MOBILE_POTRAIT_SCREEN, MOBILE_POTRAIT_SCREEN_ONLY

Theme variables are camelCase key-value pairs referenced as Theme.variableName

Styles — reusable named style definitions:
- create(object_type="style", name="cardStyle", definition={...})
- update(object_type="style", id="X", definition={...}) — partial merge""",

    "functions_schemas": """\
## Functions & Schemas — Detailed Reference

Functions — reusable KIRun function definitions:
- create(object_type="function", name="fetchUsers", namespace="MyApp", definition={name, namespace, steps, events})
- Steps: {name, namespace (e.g. "System.Context.SetStore"), parameterMap, dependentSteps}
- Event functions on pages also use KIRun steps — same format via \
update(object_type="page", event_function={function_name: "X", definition: {...}})

Schemas — data structure definitions:
- create(object_type="schema", name="UserSchema", definition={...})""",

    "data_entities": """\
## Data Entities — Detailed Reference

Connections: API connection configurations (endpoints, auth, headers)
Workflows: automation workflow definitions (triggers, steps, conditions)
Templates: message/email templates with variable substitution
URI Paths: URL routing and path parameter definitions

All use the same CRUD pattern:
- list(object_type="connection") — list all
- create(object_type="connection", name="myApi", definition={...})
- read(object_type="connection", id="X")
- update(object_type="connection", id="X", definition={...})
- delete(object_type="connection", id="X")""",

    "version_api": """\
## Version Control & API Reference — Detailed Reference

list_versions: version history for any entity (pass object_id + entity_type)
read_version: full object snapshot at a specific version
rollback_version: restore entity to a historical version (creates new version)

lookup_api: detailed endpoint info for ui, core, security, files services.
Call with just a service name to list entities. Add entity name for full endpoint details.
Use when building FetchData steps, API connections, or any data-fetching logic.""",
}

# ── Relevance keywords per group ──────────────────────────────

_GROUP_KEYWORDS: dict[str, list[str]] = {
    "page_operations": [
        "page", "pages", "component", "button", "text", "grid", "layout",
        "textbox", "dropdown", "checkbox", "radio", "image", "icon", "table",
        "tabs", "stepper", "menu", "batch", "calendar", "toggle", "textarea",
        "event", "onclick", "onchange", "onblur", "handler", "click",
        "event function", "add", "remove", "move",
    ],
    "application_workflow": [
        "app", "application", "create app", "appcode", "font", "fontpack",
    ],
    "styling": [
        "style", "theme", "color", "font", "css", "padding", "margin",
        "background", "border", "design", "dark mode", "light mode",
        "responsive", "breakpoint", "hover", "animation",
    ],
    "functions_schemas": [
        "function", "kirun", "builtin", "reusable", "step", "action",
        "navigate", "navigation", "api call", "set store", "get store",
        "schema", "data model", "data structure",
    ],
    "data_entities": [
        "connection", "workflow", "template", "uripath",
        "uri", "route", "routing", "api connection", "automation",
    ],
    "version_api": [
        "version", "history", "rollback", "undo", "revert", "restore",
        "api", "fetch", "endpoint", "rest", "http", "request",
        "fetchdata", "query data", "call api",
    ],
}

# Default groups when no keywords match
_DEFAULT_GROUPS = ["application_workflow", "page_operations"]

# Maximum number of detail groups to inject per turn
_MAX_DETAIL_GROUPS = 2

# ── Object type → group mapping (for detecting recent tool use) ──

_OBJECT_TYPE_TO_GROUP: dict[str, str] = {
    "page": "page_operations",
    "application": "application_workflow",
    "theme": "styling",
    "style": "styling",
    "function": "functions_schemas",
    "schema": "functions_schemas",
    "connection": "data_entities",
    "workflow": "data_entities",
    "template": "data_entities",
    "uripath": "data_entities",
}


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
    """Detect tool groups from recent tool calls by examining object_type arguments."""
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
            name = block.get("name", "")
            # Version and API tools map directly
            if name in ("list_versions", "read_version", "rollback_version", "lookup_api"):
                groups.add("version_api")
            else:
                # CRUD tools: detect group from object_type argument
                obj_type = block.get("input", {}).get("object_type", "")
                group = _OBJECT_TYPE_TO_GROUP.get(obj_type)
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
