# Application Builder Agent

## Purpose

An agentic AI system that builds entire no-code applications through multi-turn conversation. Like Claude Code builds codebases, this agent builds Modlix applications — creating and wiring together pages, components, styles, themes, functions, schemas, connections, workflows, templates, and routing.

## How It Works

```
User: "Build me a CRM with contacts, deals, and a dashboard"
  │
  ▼
Agent (Claude with tool-use):
  │
  ├─ Thinks: "I need to create an app, then build 4 pages..."
  │
  ├─ Calls: create_application(appName="MyCRM")
  │   └─ Result: App created with appCode "mycrm"
  │
  ├─ Calls: create_theme(name="crm-theme", variables={...})
  │   └─ Result: Theme created
  │
  ├─ Calls: create_page(name="shell", rootComponent="shell_grid", ...)
  │   └─ Result: Shell page created (navigation wrapper)
  │
  ├─ Calls: create_page(name="contacts", rootComponent="contacts_grid", ...)
  │   └─ Result: Contacts page created with form + table
  │
  ├─ Calls: create_page(name="deals", ...)
  │   └─ Result: Deals page created
  │
  ├─ Calls: create_page(name="dashboard", ...)
  │   └─ Result: Dashboard page with stats cards
  │
  ├─ Calls: update_application(properties={defaultPage: "dashboard", ...})
  │   └─ Result: App configured
  │
  └─ Responds: "I've created your CRM application with 4 pages..."
```

The user can then follow up: *"Add a priority field to deals"* → agent reads the deals page, modifies it, updates functions.

---

## Architecture

```
app/agents/appbuilder/
├── agent.py              Extends core.agent.BaseAgent
├── context.py            System prompt builder (loads aicontext/ docs)
├── catalog.py            Dynamic component catalog (fetched from CDN at startup)
├── router.py             POST /api/ai/appbuilder/chat
└── tools/
    ├── __init__.py
    ├── registry.py           All tool definitions (exports ALL_TOOLS)
    ├── _executor.py          Shared page read-modify-write executor
    ├── _shared.py            SaasClient singleton + helper utilities
    ├── page_tools.py         Page CRUD (list, create, delete, read structure/properties)
    ├── component_tools.py    Component CRUD (add, update, read, remove, move)
    ├── batch_tools.py        Batch page operations (multiple add/update/remove/move in one call)
    ├── event_tools.py        Event function CRUD (write, read, list)
    ├── application_tools.py  App CRUD + export/import
    ├── style_tools.py        Theme + style CRUD
    ├── function_tools.py     Function + schema CRUD, search builtins
    ├── entity_tools.py       Connection, workflow, template, filler, uripath, event def/action CRUD
    └── version_tools.py      Version history listing and rollback for any entity
```

---

## API Endpoint

```
POST /api/ai/appbuilder/chat
Headers:
    Authorization: Bearer {jwt_token}
    clientCode: {client_code}
    appCode: {app_code}          (target app, or "system" for app creation)
Body:
    {
        "message": "Build me a task management app",
        "session_id": null,       // null = new conversation
        "app_code": "taskapp",    // optional target app
        "attachments": []         // optional images
    }
Response:
    SSE stream (text/event-stream)
```

### SSE Events

```
event: text
data: {"content": "I'll create a task management application..."}

event: tool_start
data: {"id": "tc_1", "tool": "create_application", "input": {"appName": "TaskApp"}}

event: tool_result
data: {"id": "tc_1", "tool": "create_application", "success": true,
       "summary": "Application 'TaskApp' created with appCode 'taskapp'"}

event: tool_start
data: {"id": "tc_2", "tool": "create_theme", "input": {"name": "taskapp-theme", ...}}

event: tool_result
data: {"id": "tc_2", "tool": "create_theme", "success": true,
       "summary": "Theme 'taskapp-theme' created"}

event: text
data: {"content": "Now let me create the login page..."}

... (more tool calls and text) ...

event: done
data: {"session_id": "abc-123", "usage": {"input_tokens": 8000, "output_tokens": 3000}}
```

---

## Tool Design Philosophy

### The Problem with Simple CRUD Tools

Page definitions can be **30,000+ lines of JSON**. If the agent sees full page definitions as tool inputs/outputs, it would:
- Burn through the context window in 1-2 turns
- Struggle to reason about 30K lines of JSON
- Produce errors when generating such large outputs

### The Solution: Fine-Grained Component-Level Tools

For **large objects (pages)**, the agent works at the **component level**:
- Agent never sees the full page JSON
- `read_page_structure` returns a compact tree (component keys, types, parent relationships)
- `add_component`, `update_component` operate on individual components
- The **Python tool executor** handles the full read-modify-write internally

For **small objects (themes, connections, templates, etc.)**, standard CRUD is fine since they're compact.

### Tool Executor Pattern

```
Agent calls: add_component(pageName="login", parentKey="form_grid", component={...})
                                    │
                                    ▼
Python Tool Executor (invisible to agent):
  1. GET /api/ui/pages?name=login&appCode=mycrm  → Full 30K page JSON
  2. Insert new component into componentDefinition map
  3. Add child reference to parent's children map
  4. PUT /api/ui/pages/{id}  → Write full page back
  5. Return compact summary to agent: "Component 'email_input' (TextBox) added to 'form_grid'"
```

The agent sees only compact inputs and outputs. The executor handles the heavy lifting.

---

## Tool Definitions

### Application Tools (Standard CRUD — small objects)

| Tool | Description | Backend Call |
|------|-------------|-------------|
| `create_application` | Create a new app. Input: `appName`, optional `appCode`, `appAccessType` | `POST /api/multi/application` |
| `read_application` | Read app metadata and properties (default page, shell, language, theme) | `GET /api/ui/applications?appCode=X` |
| `update_application` | Update app properties (defaultPage, shellPage, language, translations) | `PUT /api/ui/applications/{id}` |
| `list_applications` | List all apps for the current client | `GET /api/security/applications` |
| `delete_application` | Delete an application | `DELETE /api/multi/application/{appCode}` |
| `export_application` | Export full app as JSON transport | `GET /api/multi/application/transport` |
| `import_application` | Create app from transport definition (clone/template) | `POST /api/multi/application` with `transportDefinition` |

---

### Page Tools (Fine-Grained — large objects)

Pages are the largest and most complex objects. Tools operate at **component/event level**, not full-page level.

#### Reading Pages

| Tool | Description | What Agent Sees |
|------|-------------|-----------------|
| `list_pages` | List all pages in an app | `[{name, id, rootComponent, componentCount}]` |
| `read_page_structure` | Read compact page structure (component tree + event names) | Tree of `{key, type, children, hasEvents, hasStyles}` — NOT the full 30K JSON |
| `read_component` | Read one component's full definition | Single component: `{key, type, properties, styleProperties, children, bindingPath}` |
| `read_event_function` | Read one event function's KIRun definition | Single function: `{name, steps, parameterMap}` |
| `read_page_properties` | Read page-level properties (storeInitialization, etc.) | `{storeInitialization, translations, ...}` |

**`read_page_structure` executor logic:**
```python
async def execute(self, input, context):
    # 1. Fetch full page from backend
    page = await self.client.get(f"/api/ui/pages/{input['pageId']}")

    # 2. Build compact tree (NOT the full JSON)
    tree = {}
    for key, comp in page["componentDefinition"].items():
        tree[key] = {
            "type": comp["type"],
            "children": list(comp.get("children", {}).keys()),
            "hasProperties": bool(comp.get("properties")),
            "hasStyles": bool(comp.get("styleProperties")),
            "displayOrder": comp.get("displayOrder"),
        }

    return {
        "name": page["name"],
        "id": page["id"],
        "rootComponent": page["rootComponent"],
        "componentTree": tree,
        "eventFunctions": list(page.get("eventFunctions", {}).keys()),
        "storeInitKeys": list(page.get("properties", {}).get("storeInitialization", {}).keys()),
    }
```

#### Creating Pages

| Tool | Description | What Agent Provides |
|------|-------------|-------------------|
| `create_page` | Create a new page with initial components | `{name, appCode, rootComponent, componentDefinition, eventFunctions, properties}` |

For new pages, the agent DOES provide the full component definition since it's generating from scratch. But pages start small (5-20 components) and grow over time via the fine-grained tools.

#### Modifying Pages (Component Level)

| Tool | Description | What Agent Provides |
|------|-------------|-------------------|
| `add_component` | Add a component to a page under a parent | `{pageName, appCode, parentKey, component: {key, type, properties, styleProperties}}` |
| `update_component` | Modify a component's properties or styles | `{pageName, appCode, componentKey, properties: {...}, styleProperties: {...}}` |
| `remove_component` | Remove a component (and optionally its children) | `{pageName, appCode, componentKey, removeChildren: bool}` |
| `move_component` | Move a component to a different parent | `{pageName, appCode, componentKey, newParentKey, displayOrder}` |
| `batch_update_page` | Apply multiple component operations in a single fetch+save (preferred over individual calls) | `{pageName, operations: [{op, ...}], message}` |
| `add_event_function` | Add an event function to the page | `{pageName, appCode, eventName, functionDefinition: {steps, ...}}` |
| `update_event_function` | Modify an event function | `{pageName, appCode, eventName, functionDefinition: {steps, ...}}` |
| `remove_event_function` | Remove an event function | `{pageName, appCode, eventName}` |
| `update_page_properties` | Update page-level properties (storeInitialization, etc.) | `{pageName, appCode, properties: {...}}` |
| `delete_page` | Delete a page entirely | `{pageName, appCode}` |

**`add_component` executor logic:**
```python
async def execute(self, input, context):
    # 1. Fetch the full page
    pages = await self.client.get("/api/ui/pages", params={"name": input["pageName"], "appCode": input["appCode"]})
    page = pages["content"][0]

    # 2. Add the new component to componentDefinition
    component = input["component"]
    page["componentDefinition"][component["key"]] = component

    # 3. Add child reference to parent
    parent_key = input["parentKey"]
    if "children" not in page["componentDefinition"][parent_key]:
        page["componentDefinition"][parent_key]["children"] = {}
    page["componentDefinition"][parent_key]["children"][component["key"]] = True

    # 4. Write back the full page
    result = await self.client.put(f"/api/ui/pages/{page['id']}", data=page)

    # 5. Return compact summary (NOT the full page)
    return {
        "success": True,
        "summary": f"Added {component['type']} '{component['key']}' to '{parent_key}'",
        "componentKey": component["key"],
        "parentKey": parent_key,
        "pageId": page["id"],
    }
```

---

### Theme Tools (Standard CRUD — small objects)

| Tool | Description | Backend Call |
|------|-------------|-------------|
| `create_theme` | Create theme with color/font/spacing variables | `POST /api/ui/themes` |
| `read_theme` | Read a theme's variables | `GET /api/ui/themes/{id}` |
| `update_theme` | Update theme variables | `PUT /api/ui/themes/{id}` |
| `list_themes` | List all themes in an app | `GET /api/ui/themes?appCode=X` |

---

### Style Tools (Standard CRUD — small objects)

| Tool | Description | Backend Call |
|------|-------------|-------------|
| `create_style` | Create a global style (CSS definition) | `POST /api/ui/styles` |
| `read_style` | Read a style | `GET /api/ui/styles/{id}` |
| `update_style` | Update a style | `PUT /api/ui/styles/{id}` |
| `list_styles` | List all styles in an app | `GET /api/ui/styles?appCode=X` |

---

### Function Tools (Standard CRUD — medium objects)

| Tool | Description | Backend Call |
|------|-------------|-------------|
| `create_function` | Create a KIRun function (steps, parameters) | `POST /api/ui/functions` or `POST /api/core/functions` |
| `read_function` | Read a function definition | `GET /api/ui/functions/{id}` |
| `update_function` | Update a function | `PUT /api/ui/functions/{id}` |
| `list_functions` | List all functions in an app | `GET /api/ui/functions?appCode=X` |
| `search_builtin_functions` | Search KIRun built-in functions by pattern | `GET /api/core/functions/repositoryFilter?filter=X` |

---

### Schema Tools (Standard CRUD)

| Tool | Backend Call |
|------|-------------|
| `create_schema` / `read_schema` / `update_schema` / `list_schemas` | `api/ui/schemas` + `api/core/schemas` |

### Connection Tools (Standard CRUD)

| Tool | Backend Call |
|------|-------------|
| `create_connection` / `read_connection` / `update_connection` / `list_connections` | `api/core/connections` |

Connection types: `APP_DATA`, `MAIL`, `REST_API`, `NOTIFICATION`, `TEXT`, `CALL`, `AI`
Sub-types: `MONGO`, `SENDGRID`, `SMTP`, `REST_API_BASIC`, `REST_API_AUTH`, `REST_API_OAUTH2`, `OPENAI`, `CLAUDE`, etc.

### Workflow & Automation Tools (Standard CRUD)

| Tool | Backend Call |
|------|-------------|
| `create_workflow` / `read_workflow` / `update_workflow` / `list_workflows` | `api/core/workflows` |
| `create_event_definition` / `list_event_definitions` | `api/core/eventDefinitions` |
| `create_event_action` / `list_event_actions` | `api/core/workflow/eventActions` |

### Template Tools (Standard CRUD)

| Tool | Backend Call |
|------|-------------|
| `create_template` / `read_template` / `update_template` / `list_templates` | `api/core/templates` |

### URI Path Tools (Standard CRUD)

| Tool | Backend Call |
|------|-------------|
| `create_uripath` / `read_uripath` / `update_uripath` / `list_uripaths` | `api/ui/uripaths` |

### Filler Tools (Standard CRUD)

| Tool | Backend Call |
|------|-------------|
| `create_filler` / `read_filler` / `update_filler` / `list_fillers` | `api/core/filler` |

### Version Tools

| Tool | Description | Backend Call |
|------|-------------|-------------|
| `list_versions` | List version history for any entity (page, theme, function, etc.) | `GET /api/ui/versions/{objectId}/query` or `GET /api/core/versions/{objectId}/query` |
| `rollback_version` | Rollback an entity to a previous version | Fetch historical version → PUT to entity endpoint |

Supported entity types: application, page, theme, style, function, schema, filler, uripath, connection, workflow, template.

---

## Preview System

Each tool result includes **preview data** so the Prompt component can render inline visualizations or show a preview panel next to the chat.

### SSE Preview Events

```
event: tool_result
data: {
    "id": "tc_3",
    "tool": "create_page",
    "success": true,
    "summary": "Page 'login' created with 6 components",
    "preview": {
        "type": "page",
        "name": "login",
        "appCode": "mycrm",
        "id": "abc123",
        "componentTree": {
            "root_grid": {"type": "Grid", "children": ["header", "form_grid", "footer"]},
            "header": {"type": "Grid", "children": ["logo", "title"]},
            "form_grid": {"type": "Grid", "children": ["email_input", "password_input", "submit_btn"]},
            ...
        }
    }
}
```

```
event: tool_result
data: {
    "id": "tc_2",
    "tool": "create_theme",
    "success": true,
    "summary": "Theme 'crm-theme' created",
    "preview": {
        "type": "theme",
        "name": "crm-theme",
        "variables": {
            "primaryColor": "#2563EB",
            "backgroundColor": "#FFFFFF",
            "textColor": "#1F2937",
            "fontFamily": "Inter, sans-serif"
        }
    }
}
```

```
event: tool_result
data: {
    "id": "tc_5",
    "tool": "add_component",
    "success": true,
    "summary": "Added TextBox 'email_input' to 'form_grid'",
    "preview": {
        "type": "component",
        "pageName": "login",
        "pageId": "abc123",
        "component": {
            "key": "email_input",
            "type": "TextBox",
            "parentKey": "form_grid",
            "properties": {"placeholder": {"type": "VALUE", "value": "Enter email"}}
        }
    }
}
```

### Preview Rendering in Prompt Component

The Prompt component renders previews based on the `preview.type` field:

| Type | Visualization |
|------|--------------|
| `page` | Component tree diagram + "Open in Page Editor" button. Optionally render page in an iframe preview panel. |
| `theme` | Color swatches + font preview for the theme variables |
| `style` | CSS code block preview |
| `component` | Component card (type icon + key + properties summary) |
| `function` | Step flow diagram or KIRun code block |
| `schema` | Field list / JSON Schema tree |
| `connection` | Connection type badge + details summary |
| `application` | App card (name, appCode, page count) |

For **page previews**, the most powerful option is an **iframe** pointing to the page's runtime URL, which renders the actual page using the nocode-ui engine. This gives a true WYSIWYG preview.

---

## System Prompt

The agent's system prompt is built from two sources:

### Static Context (Prompt Cached)

These `aicontext/` documents are loaded once and cached via Anthropic's ephemeral cache for ~90% token savings on subsequent turns (see `context.py:AICONTEXT_DOCS` for the full ordered list):

| Document | Content | Why |
|----------|---------|-----|
| `00-critical-rules.md` | FLAT componentDefinition, DataLocation format, style key format, valid component types | Prevents structural errors |
| `02-application-and-page-definitions.md` | App and page JSON structure | Core data model |
| `03-component-system.md` | Component registry and property system | What components exist |
| `04-property-system.md` | ComponentProperty, DataLocation types, property resolution | Property format rules |
| `05-style-system.md` | Responsive styles, pseudo-states, sub-component keys, breakpoints | How styling works |
| `06-state-management.md` | Store system, Page/Store/Theme paths | State management |
| `07-event-system.md` | Event functions and execution | How interactions work |
| `08-functions-and-actions.md` | UIEngine functions (SetStore, Navigate, Fetch, etc.) | Available actions |
| `11-data-binding.md` | Binding paths for form/data components | Data binding details |
| `15-examples-and-patterns.md` | Real-world patterns (forms, lists, responsive layouts) | Practical examples |
| `16-schema-definitions.md` | Schema definitions | Schema format |
| `17-theme-definitions.md` | Theme variable definitions | Theme format |
| `18-style-definitions.md` | Global style definitions | Style format |
| `19-function-definitions.md` | KIRun function definitions | Function format |
| `20-filler-and-uripath.md` | Filler and URI path definitions | Filler/URI format |
| `21-kirun-system-functions.md` | System functions (Math, String, Array, Date) | Available utilities |
| `22-component-reference.md` | All component types with properties, sub-components, pseudo-states | Component catalog |

### Dynamic Context (RAG per Session)

Retrieved from ChromaDB based on the user's first message:
- Relevant page/app/theme/style samples from `aicontext/samples/`
- Additional reference docs as needed

### System Prompt Persona

See `context.py:AGENT_PERSONA` for the full text. Key rules enforced:

```
Structure rules:
- componentDefinition is a FLAT map (string key → component object). Never nested.
- rootComponent is a STRING key (e.g. "root"), not an object.
- Children are stored as: {"childKey": true} in the parent's children map.
- Event functions cannot receive arguments — they read from Store.

Property format (DataLocation):
- EVERY property value MUST be a DataLocation object with a "type" field.
- Static: {"type": "VALUE", "value": "Hello"}.
- Dynamic: {"type": "EXPRESSION", "expression": "Store.user.name"}.
- WRONG: {"value": "Hello"} (missing type), "Hello" (bare string).
- onClick format: {"type": "VALUE", "value": "eventFunctionName"}.

Style properties format:
- Structure: {"<uniqueKey>": {"resolutions": {"ALL": {"<key>": {"type": "VALUE", "value": "<val>"}}}}}.
- Key format: "<subComponent>-<cssProp>:<pseudoState>" (subComponent/pseudoState optional).
- CSS props MUST be camelCase (paddingLeft, marginTop), NEVER shorthand (padding) or kebab-case.
- Each style value MUST be a DataLocation with type field.

Component types:
- Valid: Grid, Text, Button, TextBox, TextArea, Image, Icon, Dropdown, CheckBox,
  RadioButton, ToggleButton, Calendar, Table, Tabs, Stepper, Menu, etc.
- Never use: Box, Container, Div, Flex, Input, Select, Section, Span, Paragraph.
```

---

## Session Management

```python
class AppBuilderSession(BaseSession):
    """Session for the application builder agent."""
    client_code: str
    app_code: str           # Target app (may change during conversation)
    auth_token: str         # JWT for backend calls
    saas_client: SaasClient # Configured HTTP client for nocode-saas
    created_apps: list[str] # Apps created in this session (for tracking)
```

- **In-memory** during active conversation
- **Persisted to MySQL** on completion (session_id, messages, token usage)
- **Context limit**: ~180K tokens. When approaching limit, summarize older messages.

---

## Error Handling

| Error | Agent Behavior |
|-------|---------------|
| 401 Unauthorized | Tell user their session has expired, suggest re-authenticating |
| 403 Forbidden | Tell user they don't have permission for this operation |
| 404 Not Found | Object doesn't exist — check if name/ID is correct, or create it |
| 409 Conflict | Object already exists — read it and modify instead of creating |
| 422 Validation Error | Extract error details, fix the input, retry the tool call |
| 500 Server Error | Tell user there's a backend issue, suggest trying again |

The agent should NOT retry indefinitely. After 2 failed attempts at the same tool, explain the error and ask the user for guidance.

---

## Example Conversations

### Build a New App

```
User: Build me a simple blog with a home page showing posts and a page to create new posts.

Agent: I'll build a blog application for you. Let me start by creating the app and setting up the theme.

[tool_call: create_application(appName="My Blog")]
[tool_result: App created with appCode "myblog"]

[tool_call: create_theme(name="blog-theme", variables={primaryColor: "#2563EB", ...})]
[tool_result: Theme created]

Now let me create the home page with a post listing...

[tool_call: create_page(name="home", rootComponent="home_grid", componentDefinition={...})]
[tool_result: Page "home" created with 8 components]

[tool_call: create_page(name="create-post", rootComponent="form_grid", componentDefinition={...})]
[tool_result: Page "create-post" created with form and submit button]

[tool_call: update_application(properties={defaultPage: "home", ...})]
[tool_result: App configured with default page]

Your blog is ready! Here's what I built:
- **Home page** — Shows a list of blog posts with title, date, and excerpt
- **Create Post page** — Form with title, content (textarea), and publish button
- **Theme** — Clean blue theme with responsive typography
```

### Modify Existing App

```
User: Add a dark mode toggle to the blog

Agent: Let me read the current theme and page structure to understand what we're working with.

[tool_call: read_theme(appCode="myblog")]
[tool_result: Theme with primaryColor="#2563EB", backgroundColor="#FFFFFF", textColor="#1F2937"]

[tool_call: read_page_structure(pageName="home", appCode="myblog")]
[tool_result: Tree — root: home_grid (Grid) → [header (Grid), post_list (Grid), footer (Grid)],
              header → [logo (Image), title (Text), nav_links (Grid)]]

I can see the header has a nav_links grid. I'll add dark mode variables to the theme and a toggle
button in the header.

[tool_call: update_theme(id="...", variables={...add darkBg, darkText, darkPrimary variants...})]
[tool_result: Theme updated with dark mode variables]

[tool_call: add_component(pageName="home", appCode="myblog", parentKey="nav_links",
            component={key: "dark_toggle", type: "ToggleButton",
                       properties: {label: {type: "VALUE", value: "🌙"},
                                    onClick: {type: "VALUE", value: "toggleDarkMode"}}})]
[tool_result: Added ToggleButton 'dark_toggle' to 'nav_links']

[tool_call: add_event_function(pageName="home", appCode="myblog", eventName="toggleDarkMode",
            functionDefinition={steps: {setDarkMode: {name: "SetStore", ...}}})]
[tool_result: Event function 'toggleDarkMode' added]

Done! I've added:
- **Dark mode theme variables** — darkBg, darkText, darkPrimary alongside the existing light values
- **Toggle button** in the header nav area
- **Event function** that switches between light/dark by updating a store variable
- The toggle button triggers `toggleDarkMode` which sets `Page.isDarkMode` in the store
```

### Add Components to Existing Page

```
User: Add a search bar to the blog home page above the post list

Agent: Let me see the current page structure first.

[tool_call: read_page_structure(pageName="home", appCode="myblog")]
[tool_result: Tree — home_grid → [header, post_list, footer]]

I'll add a search section between the header and post list.

[tool_call: add_component(pageName="home", appCode="myblog", parentKey="home_grid",
            component={key: "search_section", type: "Grid", displayOrder: 1,
                       children: {}, styleProperties: {...}})]
[tool_result: Added Grid 'search_section' to 'home_grid']

[tool_call: add_component(pageName="home", appCode="myblog", parentKey="search_section",
            component={key: "search_input", type: "TextBox",
                       properties: {placeholder: {type: "VALUE", value: "Search posts..."},
                                    onChange: {type: "VALUE", value: "filterPosts"}}})]
[tool_result: Added TextBox 'search_input' to 'search_section']

[tool_call: add_event_function(pageName="home", appCode="myblog", eventName="filterPosts",
            functionDefinition={steps: {filter: {name: "SetStore",
                                parameterMap: {path: {type: "VALUE", value: "Page.searchQuery"}, ...}}}})]
[tool_result: Event function 'filterPosts' added]

Done! I added a search section with a text input that filters posts via a store variable.
The search bar sits between the header and the post list.
```

---

## Metrics & Tracking

Each session tracks:
- `total_input_tokens` / `total_output_tokens` — LLM token usage
- `total_cache_read_tokens` / `total_cache_creation_tokens` — Prompt caching efficiency
- `request_count` — Number of user messages
- `turn_count` — Total agent turns (including tool-use loops)
- `tools_called` — List of tools invoked and their success/failure counts

---

## Dependencies on Other Systems

| System | How Used | Required? |
|--------|----------|-----------|
| nocode-saas Gateway (8080) | All tool executions route through Gateway | Yes |
| Security service (8003) | App listing, client info | Yes |
| UI service (8002) | Pages, styles, themes, applications | Yes |
| Core service (8001) | Functions, schemas, connections, workflows | Yes |
| Multi service (8009) | App creation/deletion, transport | Yes |
| ChromaDB | RAG retrieval for system prompt context | Optional (degrades prompt quality) |
| Redis | Rate limiting | Optional (disabled if unavailable) |
| MySQL | Session tracking | Optional (sessions still work in-memory) |
