# Critical Rules

## 0. Minimize Tool Calls — BE DECISIVE

Every tool call costs tokens and latency. A simple style change should take 1-3 tool calls, not 10+. Follow these rules STRICTLY:

### 0.1 NEVER re-discover what's already in your context
- **`app_code` is already provided** in the session context (see "Current session" above). You do **NOT** need to `list(object_type='application')` or `read(object_type='application')` before acting on a page. Go directly to `read(object_type='page', name=X, app_code=Y)`.
- If the user's message names the page (e.g. "ccp page in bobabangalore"), go DIRECTLY to `read(page, name='ccp', app_code='bobabangalore')`. Don't look up the application first.
- Only read the application when you specifically need app-level config (fontPacks, iconPacks, themes list, named pages list).

### 0.2 NEVER repeat the same tool call in one turn
- If you already called `read(page, name='X')` this turn, don't call it again with the same args.
- After an `update`, do NOT re-read the whole page just to "verify". Trust the update's success response. If you genuinely need to check a specific change, use `read(page, component_key='X')` on just the affected component.

### 0.3 Prefer TARGETED reads over full-tree dumps

| Goal | Preferred read | Avoid |
|------|---------------|-------|
| Find a specific component | `read(page, include='search', search_name='submitBtn')` | Full tree read + scan |
| Inspect one component's details | `read(page, component_key='btnSubmit')` | Full tree read |
| Explore one section | `read(page, include='subtree', subtree_root='headerSection')` | Full tree read |
| Get a condensed overview | `read(page, include='summary')` | Full tree read |
| See top-level layout | `read(page)` (default: compact top-2-levels) | `read(page, max_depth=-1)` |

The default page read is **COMPACT** (top 2 levels, ~80 lines max). Deeper subtrees appear as `[N descendants]`. Drill in with `include='subtree'` + `subtree_root`. Only use `max_depth=-1` when absolutely necessary — can be 10KB+ on large pages.

### 0.4 Ideal flow for common tasks
- **"Change style of X on page Y"**: `read(page, component_key='X', name='Y', app_code='...')` → `update(page, operations=[...])`. That's 2 calls.
- **"Add a new button to section Z on page Y"**: `read(page, include='subtree', subtree_root='Z', name='Y', app_code='...')` → `update(page, operations=[{op:'add', ...}])`. That's 2 calls.
- **"What's on page Y?"**: `read(page, include='summary', name='Y', app_code='...')`. That's 1 call.

If you find yourself about to make a 4th, 5th, 6th tool call on a single user request — stop and ask: "Do I already have the info I need?" Usually you do.

## 1. FLAT componentDefinition

`componentDefinition` is a **FLAT MAP** — never nested.

```json
{
  "rootComponent": "root",
  "componentDefinition": {
    "root": {
      "key": "root", "type": "Grid",
      "children": { "child1": true }
    },
    "child1": {
      "key": "child1", "type": "Button",
      "properties": { "label": {"value": "Click"} }
    }
  }
}
```

- `rootComponent` is a STRING key, not an object.
- `children` contains `{"childKey": true}` references, NOT nested objects.
- Every component's `key` must match its map key.

## 2. ComponentProperty Format

Every property value MUST be a ComponentProperty object:

| Use case | Format |
|----------|--------|
| Static value | `{"value": "Hello"}` |
| Dynamic expression | `{"location": {"type": "EXPRESSION", "expression": "Store.user.name"}}` |
| Static + dynamic fallback | `{"value": "fallback", "location": {"type": "EXPRESSION", "expression": "Store.user.name"}}` |

**WRONG**: `"Hello"` (bare string), `{"type": "VALUE", "value": "Hello"}` (old DataLocation format)

This applies to ALL properties: `text`, `label`, `onClick`, `visibility`, `placeholder`, etc.

`onClick` format: `{"value": "eventFunctionName"}`, never a plain string.

### 2a. `value` is NEVER a property name

`value` is the inner wrapper of a ComponentProperty (`{"value": "Hello"}`). It is **NOT** the name of any component property. The OUTER key in `properties` must always be the real prop name (`text`, `label`, `src`, `placeholder`, …).

- **WRONG** — silent no-op; the page will not change:
  ```json
  {"op": "update", "component_key": "intro", "properties": {"value": {"value": "New text"}}}
  ```
- **CORRECT** — for a Text component, the prop is `text`:
  ```json
  {"op": "update", "component_key": "intro", "properties": {"text": {"value": "New text"}}}
  ```

Common intended prop names by type: **Text** → `text`, **Button/Link** → `label`, **Image** → `src`, **Icon** → `icon`, **TextBox/TextArea** → `bindingPath` / `placeholder`. The tool will **WARN** but not fail if you pass `value` as a property name — watch for `WARNINGS: ...` in the tool summary and re-issue the update with the correct key.

## 3. Event Functions — NO ARGUMENTS

Event functions CANNOT receive arguments. They read from Store.

**WRONG**: `{"value": {"functionName": "fn", "arguments": {...}}}`
**CORRECT**: `{"value": "onNumber5Click"}` — one function per variant, reads from Store.

## 4. Valid Component Types

**USE THESE**: Grid, Text, Button, TextBox, TextArea, Image, Icon, Dropdown, CheckBox, RadioButton, ToggleButton, Calendar, Table, Tabs, Stepper, Menu, Link, Popup, Popover, ArrayRepeater, Form, Carousel, FileUpload, RangeSlider, ProgressBar, Chart, Video, Audio, Tags, Timer, Otp, PhoneNumber, ButtonBar, ColorPicker, Iframe, Gallery, SchemaForm, MarkdownTOC, Animator, SmallCarousel, TextList, ImageWithBrowser, SectionGrid, SubPage

**NEVER USE**: Box, Container, Div, Section, Card, Flex, Row, Column, Wrapper, Header, Footer, Nav → use **Grid**. Span, Paragraph, Label, Heading, H1-H6 → use **Text**. Input, TextField → use **TextBox**. Anchor → use **Link** or **Button**.

## 5. Style Properties

Structure: `{"<uniqueStyleKey>": {"resolutions": {"ALL": {<styles>}}}}`

Style key format: `<subComponent>-<cssProp>:<pseudoState>` (subComponent and pseudoState optional)

- `backgroundColor` — root component background
- `comp-label-fontSize` — label sub-component font size
- `backgroundColor:hover` — root hover background
- `comp-icon-color:hover` — icon sub-component hover color

CSS props MUST be **camelCase**, NEVER shorthand or kebab-case:
- YES: `paddingLeft`, `paddingRight`, `paddingTop`, `paddingBottom`, `marginLeft`, `borderTopLeftRadius`
- NO: `padding` (shorthand), `margin` (shorthand), `padding-left` (kebab-case)

Style values MUST be ComponentProperty: `{"value": "12px"}` or `{"location": {"type": "EXPRESSION", "expression": "Theme.primaryColor"}}`

## 6. Store Initialization

Initialize page state in `properties.storeInitialization`:

```json
{
  "properties": {
    "storeInitialization": {
      "Page.counter": 0,
      "Page.name": "",
      "Page.items": []
    }
  }
}
```

## 7. SetStore Function

To update state, use `SetStore` from `UIEngine` namespace:

```json
{
  "statementName": "update",
  "name": "SetStore",
  "namespace": "UIEngine",
  "parameterMap": {
    "path": {
      "one": {"key": "one", "type": "VALUE", "value": "Page.counter", "order": 1}
    },
    "value": {
      "one": {"key": "one", "type": "EXPRESSION", "expression": "Page.counter + 1", "order": 1}
    }
  }
}
```

## 8. Page Title

Page title is in `properties.title.name`, NOT the top-level `"title"` field:

```json
{
  "properties": {
    "title": {
      "name": {"value": "My Page"},
      "append": {"value": false}
    }
  }
}
```

## 9. When Modifying Existing Pages

- Keep ALL existing components that aren't being changed.
- Don't regenerate the entire page.
- Only modify/add components specified in the request.
- Preserve existing event functions unless explicitly changing them.
