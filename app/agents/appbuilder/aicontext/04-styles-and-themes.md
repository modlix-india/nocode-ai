# Styles and Themes

This file is the **JSON shape**: where styleProperties live, the key format,
breakpoints, pseudo-states. For *which* of these to reach for, read
`platform_doc_read("design_system")` — the theme-vs-inline decision, the
per-component enum catalog, `Text` roles, and the traps.

## Component StyleProperties

Structure: each component has `styleProperties` with unique style keys.

```json
{
  "styleProperties": {
    "uniqueKey123": {
      "resolutions": {
        "ALL": {
          "backgroundColor": {"value": "#4F46E5"},
          "paddingLeft": {"value": "12px"},
          "paddingRight": {"value": "12px"},
          "paddingTop": {"value": "8px"},
          "paddingBottom": {"value": "8px"},
          "backgroundColor:hover": {"value": "#4338CA"},
          "comp-label-fontSize": {"value": "14px"},
          "comp-icon-color:hover": {"value": "#fff"}
        },
        "MOBILE_POTRAIT_SCREEN_ONLY": {
          "paddingLeft": {"value": "8px"},
          "paddingRight": {"value": "8px"}
        }
      }
    }
  }
}
```

### Style Key Format

`<subComponent>-<cssProp>:<pseudoState>`

- `backgroundColor` — root, no pseudo-state
- `comp-label-fontSize` — "label" sub-component
- `backgroundColor:hover` — root hover state
- `comp-icon-color:hover` — "icon" sub-component hover

### CSS Property Rules

- MUST be camelCase: `paddingLeft`, `marginTop`, `borderTopLeftRadius`, `fontSize`
- NEVER shorthand: `padding`, `margin`, `border`, `borderRadius`
- NEVER kebab-case: `padding-left`, `margin-top`
- Use individual sides: `paddingLeft` + `paddingRight` + `paddingTop` + `paddingBottom`

### Dynamic Style Values

```json
{
  "width": {"location": {"type": "EXPRESSION", "expression": "Theme.sidebarWidth"}},
  "color": {"location": {"type": "EXPRESSION", "expression": "Theme.primaryColor"}}
}
```

## Responsive Breakpoints

| Resolution | Description | Width |
|-----------|-------------|-------|
| `ALL` | Base styles | Any |
| `WIDE_SCREEN` | Wide screens | > 1920px |
| `DESKTOP_SCREEN` | Desktop+ | > 1280px |
| `DESKTOP_SCREEN_ONLY` | Desktop only | 1025-1280px |
| `TABLET_LANDSCAPE_SCREEN` | Tablet landscape+ | > 1024px |
| `TABLET_LANDSCAPE_SCREEN_ONLY` | Tablet landscape only | 961-1024px |
| `TABLET_POTRAIT_SCREEN` | Tablet portrait+ | > 768px |
| `TABLET_POTRAIT_SCREEN_ONLY` | Tablet portrait only | 641-960px |
| `MOBILE_LANDSCAPE_SCREEN` | Mobile landscape+ | > 480px |
| `MOBILE_LANDSCAPE_SCREEN_ONLY` | Mobile landscape only | 481-640px |
| `MOBILE_POTRAIT_SCREEN` | Mobile portrait+ | > 320px |
| `MOBILE_POTRAIT_SCREEN_ONLY` | Mobile portrait only | < 480px |

Breakpoints cascade — `DESKTOP_SCREEN` applies to all > 1280px.

## Conditional Styles

```json
{
  "styleProperties": {
    "darkStyle": {
      "condition": {"location": {"type": "EXPRESSION", "expression": "Store.isDarkMode"}},
      "resolutions": {
        "ALL": {
          "backgroundColor": {"value": "#1a1a1a"},
          "color": {"value": "#ffffff"}
        }
      }
    }
  }
}
```

## Pseudo-States

Available: `hover`, `focus`, `active`, `disabled`, `visited`, `readonly`

Used as suffix in style keys: `backgroundColor:hover`, `opacity:disabled`

Not all components support all states — check component reference for supported pseudo-states.

## Theme Definitions

Themes provide design tokens accessible via `Theme.` prefix.

### Theme Structure

Two kinds of variable live here, and they behave differently:

1. **Names the components already read** — `colorOne`, `fontColorOne`,
   `backgroundColorOne`, `primaryFont`, and the sprayed per-variant names like
   `buttonPaddingDefaultPrimary`. Setting these styles the components
   automatically, with no page change at all. The name has to match the pattern
   the component declares or it does nothing. See `design_system`.
2. **Arbitrary names of your own** — reachable only through a `Theme.` expression
   (below). Useful, but they style nothing by themselves.

The example here is the second kind.

```json
{
  "name": "appTheme",
  "variables": {
    "ALL": {
      "primaryColor": "#3B82F6",
      "textColor": "#1F2937",
      "backgroundColor": "#FFFFFF",
      "fontFamily": "'Inter', sans-serif",
      "fontSize": "16px",
      "borderRadius": "8px",
      "spacing": "16px",
      "shadowMd": "0 4px 6px rgba(0,0,0,0.1)"
    },
    "MOBILE_POTRAIT_SCREEN_ONLY": {
      "fontSize": "14px",
      "spacing": "12px"
    }
  }
}
```

### Using Theme Variables

In styles: `{"location": {"type": "EXPRESSION", "expression": "Theme.primaryColor"}}`
In properties: `{"location": {"type": "EXPRESSION", "expression": "Theme.fontFamily"}}`

This works in a page style leaf and is the right way to stay on-palette when you
genuinely must style one instance. The `<varName>` syntax does NOT work in a page
leaf — that substitution runs on theme values only. `Theme.` returns the variable's
raw value, so one whose value is itself `<anotherVar>` comes back unexpanded.

### Linking Theme to Application

```json
{"properties": {"themes": {"uuid1": {"name": "appTheme"}}}}
```

## Global Style Definitions

Application-wide CSS stylesheets injected as `<style>` tags:

```json
{
  "name": "globalStyle",
  "styleString": "* { transition: all 0.3s ease; } ::-webkit-scrollbar { width: 8px; }"
}
```

Linked in application: `{"properties": {"styles": {"uuid1": {"name": "globalStyle"}}}}`

## Page-Level CSS Classes

Pages can define custom CSS via `properties.classes`:

```json
{
  "properties": {
    "classes": {
      "uuid1": {
        "key": "uuid1",
        "selector": "@keyframes fadeIn",
        "style": "from { opacity: 0; } to { opacity: 1; }"
      }
    }
  }
}
```

## Style Application Order

1. Browser defaults → 2. Global styles → 3. Page CSS classes → 4. Component styleProperties → 5. Pseudo-states → 6. Conditional styles
