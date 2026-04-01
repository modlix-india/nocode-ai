# Critical Rules

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
