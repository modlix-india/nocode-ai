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

**Write the first kind.** A theme built only out of invented names is inert: it
looks like a theme, reads back fine, and styles nothing. `primaryColor`,
`textColor`, `backgroundColor` and `fontFamily` are the four the model reaches
for most and **none of them exist** — nothing in the entire style surface reads
them. The primary colour is `colorOne`.

The real names, which is what a theme should mostly contain:

| Role | Variable |
|---|---|
| Primary / accent | `colorOne` (then `colorTwo` … `colorFifteen`) |
| Body text | `fontColorOne` |
| Text on an accent fill | `fontColorTwo` |
| Surfaces | `backgroundColorOne` … `backgroundColorNine` |
| Page ground | `bodyBackground` |
| Hover washes | `backgroundHoverColorOne` … (pair with the `colorX` of the same rank) |
| Borders | `borderColorOne` … `borderColorSeven` |
| Fonts | `bodyFont`, then `primaryFont` … `senaryFont` (six slots) |
| Status | `successColor`, `errorColor`, `warningColor`, `informationColor` |
| Grid gap | `gapBetween` |

```json
{
  "name": "appTheme",
  "variables": {
    "ALL": {
      "colorOne": "#3B82F6",
      "colorTwo": "#1E40AF",
      "fontColorOne": "#1F2937",
      "backgroundColorOne": "#FFFFFF",
      "bodyBackground": "#FFFFFF",
      "backgroundHoverColorOne": "#A1C4FA",
      "bodyFont": "16px/24px 'Inter', sans-serif",
      "primaryFont": "40px/48px 'Space Grotesk', sans-serif",
      "gapBetween": "0px"
    }
  }
}
```

**Always set the hover washes when you set the colours.** Unset,
`backgroundHoverColorOne` resolves to its stock `#A8DEC9` — a mint green — and
that is what paints the open dropdown's highlighted row and ButtonBar's primary
hover. On any palette that is not green it looks like a bug, because it is one.
A good value is the matching `colorX` mixed about 45% toward white, which is how
the stock values themselves were derived. `create_theme` fills these in if you
omit them, but choosing them deliberately is better.

**Always set `gapBetween`.** The platform default is `5px` on *every* grid in the
app. Full-bleed sections stacked in a page root then show a 5px stripe of the page
background between them — on a dark site that reads as white seams across the
whole page. Set `0px` at the theme and give the grids that genuinely want space
(form rows, card decks) an explicit per-instance gap.

**Always choose fonts. A site on the stock face looks like a template**, however
good the rest of it is, and until now every generated site shipped that way.

Two things have to line up, and doing one without the other achieves nothing:

1. **The pack** — `app.properties.fontPacks`, a UUID-keyed map of `{name, code}`
   where `code` is literal HTML injected into the page head. This is what
   *downloads* the font. `create_app` seeds it empty, so unless something fills
   it no webfont is ever fetched.
2. **The tokens** — `bodyFont` and `primaryFont` … `senaryFont` on the theme.
   These are the CSS `font` **SHORTHAND**, so they need a size:
   `"16px/24px 'Inter', sans-serif"`. A family-only value such as
   `"'Inter', sans-serif"` is invalid and the whole declaration is discarded —
   the quiet way a theme names a font and still renders in the default one.

The easy path is to let `create_theme` do both: pass `font_pairing` and it picks
the families, writes all seven tokens and registers the pack in one call.

| `font_pairing` | Faces | Suits |
|---|---|---|
| `editorial` | Fraunces + Inter | Food, craft, retail |
| `modern` | Space Grotesk + Inter | Software, engineering |
| `classic` | Playfair Display + Source Sans 3 | Law, finance, luxury |
| `friendly` | Poppins + Inter | Consumer, education |
| `neutral` | Inter throughout | No brand voice yet |

For a family outside that list pass `font_display` (and optionally `font_body`)
with any Google Fonts family name. Omit all three and `editorial` is applied,
with a note saying so.

**Six slots exist so text can differ.** `primaryFont` and `secondaryFont` take
the display face for headings; the rest stay on the body face so buttons, labels
and captions do not inherit a display serif. Do not put one family in all seven.

If you write the tokens by hand, register the pack yourself in the same turn —
`create_theme` only registers one when it chose the fonts, because it cannot
know which family you meant.

Arbitrary names of your own are still allowed, but they are the second kind: only
a `Theme.` expression reads them, and they style nothing on their own.

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
