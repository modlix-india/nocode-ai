# Modlix component layout: properties vs styles vs bindings

A trap that bit homeTwo's rebuild on 2026-05-18: I tried to lay out a page with
raw CSS `display: flex` / `flexDirection: row` / `gridTemplateColumns` via
`patch_component_styles`. **None of those took effect.** The result was every
section stacked vertically out of order — a 33-style-rule pass that visually
contributed nothing.

Modlix components have THREE orthogonal control surfaces. Knowing which to use
is the whole game.

## The three surfaces

### 1. `properties` (component-typed inputs)

These are the typed slots the component's React implementation reads. They are
**the only way to control structural behavior** — layout direction, column
count, text level (H1/H2/…), button variant, link href, ArrayRepeater
datasource. Set via the platform's `patch_component_props` MCP tool.

**Discovery**: call `get_component_schema(component_type)` BEFORE designing.
Don't guess property names. Don't copy property names from CSS.

### 2. `styleProperties` (CSS rules)

UUID-keyed rules whose `resolutions[breakpoint][leaf-cssProp].value` holds
actual CSS values. Use for **visual** concerns only: colors, background,
borders, font size, spacing, shadows. Set via the platform's
`patch_component_styles` MCP tool.

**Don't set CSS layout properties here** (`display`, `flex-direction`,
`grid-template-columns`, etc.). The component's typed `properties` already
control layout; mixing CSS on top produces unpredictable results because the
component's React layer may overwrite your CSS at render time.

#### CRITICAL — bundle leaves into ONE rule per scope

The platform's runtime style resolution
([nocode-ui/.../useDefinition/commons.ts:50-55](../../nocode-ui/ui-app/client/src/components/util/useDefinition/commons.ts#L50-L55)) does:
```ts
if (ecs.condition) pseudoStates[state].conditioned.push(pTargets);
else pseudoStates[state].defaultOne = pTargets;  // ← OVERWRITES
```

So unconditioned rules with the same `pseudoState` (the most common case)
**SILENTLY OVERWRITE each other** — only the last UUID-rule processed survives.
Conditioned rules merge cleanly (the `.push` branch); unconditioned ones do not.

This means: do NOT create one UUID per CSS prop. Bundle every leaf you want
applied into a SINGLE rule's `resolutions[<bp>]` block:

```json
"<single-uuid>": {
  "resolutions": {
    "ALL": {
      "width":      {"value": "160px"},
      "height":     {"value": "160px"},
      "backgroundColor": {"value": "#FFFBEB"},
      "padding":    {"value": "32px"},
      "image-width":  {"value": "180px"},
      "image-height": {"value": "180px"}
    }
  }
}
```

The CFA `patch_component_styles` tool was historically creating one UUID
per leaf, which made all-but-the-last leaves invisible. Fixed 2026-05-18:
the tool now finds the existing unconditioned+matching-pseudoState rule
and merges into its `resolutions[<bp>]` block — minting a new UUID only
when no such rule exists. Implementation lives in
[`app/agents/appbuilder/tools/modlix/pages.py`](../../tools/modlix/pages.py)
(the page-composition surface).

When inheriting a page authored before the fix, **consolidate first**:
group all unconditioned rules per (pseudoState) into one UUID. The on-disk
fix script is one Python loop walking `componentDefinition` (see the
homeTwo consolidation pass in the 2026-05-18 session).

#### Leaf-key parsing — sub-component prefix matters

Leaf-key encoding (`processTargets` at
[useDefinition/commons.ts:136-141](../../nocode-ui/ui-app/client/src/components/util/useDefinition/commons.ts#L136)):

| Leaf key | Goes to | Notes |
|---|---|---|
| `width` | `resolvedStyles.comp.width` | Default sub-component is `comp` (wrapper div) |
| `image-width` | `resolvedStyles.image.width` | Sub-component prefix is everything before the first `-` |
| `cardHead-fontSize` | `resolvedStyles.cardHead.fontSize` | Whatever sub-component the component declares |

CamelCase is mandatory — `flex-direction: row` would be parsed as
sub-component `flex` + prop `direction: row` — almost certainly wrong.

### 3. `bindingProperties` / `bindingPaths`

For data-driven values. ArrayRepeater's `datasource`, dropdown's `options`,
Text's `text` when it should reflect a Store path, etc. Set via
`patch_component_bindings`.

## Cheat sheet — what's a property vs a style?

| You want… | Surface | Example |
|---|---|---|
| Lay out children horizontally | `properties` | Grid.layout = "ROWLAYOUT" |
| Responsive 3-column grid (with mobile fold) | `properties` | Grid.layout = "THREECOLUMNSLAYOUT" |
| Heading vs paragraph | `properties` | Text.textType = "H1" / "PARAGRAPH" |
| Outlined button | `properties` | Button.designType = "_outlined" |
| Image source | `properties` | Image.src = "/api/files/…" |
| Clickable Grid (route) | `properties` | Grid.linkPath = "/apps", Grid.target = "_blank" (see [reference_link_paths.md](reference_link_paths.md): NEVER the full `/appCode/clientCode/page/...` form) |
| Semantic HTML tag for Grid | `properties` | Grid.containerType = "NAV" / "HEADER" / etc. |
| Background color | `styleProperties` | backgroundColor: "#FFFBEB" |
| Padding / margin | `styleProperties` | padding: "96px 48px" |
| Gap between Grid children | `styleProperties` | gap: "24px" (Grid has NO gap prop) |
| Font weight, size, color | `styleProperties` | fontWeight: "800", fontSize: "56px", color: "#1F2937" |
| Border-radius | `styleProperties` | borderRadius: "12px" |
| Drop shadow | `styleProperties` | filter: "drop-shadow(…)" |
| Bind text to Page.foo | `bindingPaths` | value → expression `Page.foo` |

## Layout containers

### Grid

The bread-and-butter layout container. Its `layout` property is THE structural
control — and the only place you can specify multi-column responsive arrangement.

**`Grid.layout` enum** (default `SINGLECOLUMNLAYOUT` — yes, even though the
component is called "Grid", the default lays children OUT vertically):

| Value | Behavior |
|---|---|
| `SINGLECOLUMNLAYOUT` | Default. Children stack vertically. |
| `ROWLAYOUT` | Children in a horizontal row, no responsive fold. |
| `ROWCOLUMNLAYOUT` | Row on desktop, column on mobile. |
| `TWOCOLUMNSLAYOUT` | 2 cols desktop, 1 col mobile. |
| `THREECOLUMNSLAYOUT` | 3 desktop, 2 tablet, 1 mobile. |
| `FOURCOLUMNSLAYOUT` | 4 desktop, 2 tablet, 1 mobile. |
| `FIVECOLUMNSLAYOUT` | 5 desktop, 2 tablet, 1 mobile. |

Responsive behavior is **baked into the layout name**. You don't pick
"3 columns" and then add breakpoints — `THREECOLUMNSLAYOUT` already folds.

Other Grid properties (full list in catalog): `containerType` (HTML tag),
`linkPath` + `target` (clickable navigation), `visibility`,
`onClick / onMouseEnter / onMouseLeave / onEnteringViewport / onLeavingViewport`,
drag-and-drop slots (`dragData*`, `dropData*`, `onDropData`).

Notably MISSING from Grid (don't try to set these as properties): `gap`,
`columns`, `columnGap`, `rowGap`, `padding`, `direction`. Use styleProperties
(`gap`, `padding`) for spacing.

### Flex

Lower-level flexbox container. Use when you need `justifyContent: 'space-between'`
or `alignItems: 'center'` precisely. Properties: `direction` (ROW/COLUMN/ROW_REVERSE/COLUMN_REVERSE),
`wrap` (WRAP/NOWRAP/WRAP_REVERSE), `justifyContent`, `alignItems`, `gap`.

Grid is the more common choice; Flex is for cases where Grid's responsive
layout enum doesn't fit (e.g. header-bar with "logo left, nav right, sign-in
far right" — that's Flex with justifyContent: 'space-between').

## Component sub-components — `width`/`height` on Image goes on sub_component='image'

Components like Image render a wrapper `<div class="comp compImage">` AROUND
the actual content element (`<img>`). Each sub-element has its own style slot,
keyed by sub-component name:

| Component | Sub-components | When to target which |
|---|---|---|
| Image | `comp` (wrapper div), `image` (the <img>), `zoomPreview`, `magnifier`, `sliderLine`, `sliderHandle`, `tooltip` | **`image`** for the actual picture's width/height/object-fit/border-radius. `comp` for shadow, margin, positioning. |
| Button | `comp`, `label`, `leftIcon`, `rightIcon` | `label` for text color/font; `comp` for bg/border/padding |
| Text | `comp` | Single sub; just use the default |
| Grid | `comp`, `child` (cross-cuts every direct child) | rarely overridden; `comp` is fine |

If your style "doesn't apply" — width on Image being a frequent example — the
fix is almost always `sub_component=<the inner element>`, e.g.:

```python
patch_component_styles(
    page_name='homeTwo', component_key='heroIcon',
    css_props={'width': '160px', 'height': '160px', 'objectFit': 'contain'},
    sub_component='image',  # ← targets the inner <img>, not the wrapper
)
```

Discovery: grep the component's `.tsx` for `subComponentName="..."` to find the
real targetable inner elements. (Most components define 1-3 sub-components.)

## Page-level: shell wrap vs full-bleed (`properties.wrapShell`)

Every page renders one of two ways at the top level:

- **App page (default)** — `properties.wrapShell !== false`. The page renders
  INSIDE the application's `shellPage` (header + sidebar + chrome). This is
  right for editor screens, dashboards, settings, anything that lives within
  the logged-in app experience.
- **Webpage / landing / public** — `properties.wrapShell = false`. The page
  renders standalone with no shell — full-bleed, edge-to-edge. Right for
  marketing pages, landing pages, login, public-facing content.

Where it's read:
[RenderEngineContainer.tsx:328-336](../../nocode-ui/ui-app/client/src/Engine/RenderEngineContainer.tsx#L328-L336):
```ts
const { properties: { wrapShell = true } = {} } = pageDefinition;
if (wrapShell && shellPageDefinition && ...) {
    return <Page pageDefinition={shellPageDefinition} ... />;
}
return <Page pageDefinition={pageDefinition} ... />;
```

When authoring a marketing-style home page (like the App Builder's `homeTwo`),
flip `wrapShell` to false. Otherwise the sidebar/header chrome of `shellPage`
shows up and competes with your hero.

To set via the MCP: pass `properties={'wrapShell': False}` to `update_page`
(the tool merges this into the page's `properties` block).

## Sibling order — set `displayOrder`, don't rely on insertion

When two children of the same parent both have `displayOrder = 0` (the default
from `add_component` when you don't pass one), Modlix renders them in
**alphabetical key order**, not insertion order. So adding `cardEditor_head`
then `cardEditor_body` will render the body BEFORE the head (because
`cardEditor_body` < `cardEditor_head` lexicographically).

Fix: always pass `display_order: 1, 2, 3, ...` to `add_component`, OR call
`move_component(component_key, new_parent_key=<same>, display_order=N)` after
the fact. The composition_v2 `add_component` accepts `display_order` but it's
optional — make it part of the build pattern, not an afterthought.

Sister components that were added in time-sequence:
```
1. add_component(parent='heroCtas', name='CtaOpenApps', component_key='ctaOpenApps')
2. add_component(parent='heroCtas', name='CtaBuildAi',  component_key='ctaBuildAi')
```
…rendered with `ctaBuildAi` LEFT and `ctaOpenApps` RIGHT, because
`ctaBuildAi` < `ctaOpenApps` and both displayOrder were 0.

With explicit ordering:
```
1. add_component(... component_key='ctaOpenApps', display_order=1)
2. add_component(... component_key='ctaBuildAi',  display_order=2)
```
renders ctaOpenApps left, ctaBuildAi right — as authored.

## Text variants

`Text.textType` enum: `H1 | H2 | H3 | H4 | H5 | H6 | PARAGRAPH | SPAN`.
The component renders the appropriate HTML tag, and the theme provides
default typography per variant. Don't try to fake H1 by cranking
`fontSize: 56px` on a SPAN.

## Button variants

`Button.designType` enum: `_outlined | _text | _iconButton | _fabButton | _decorative`.
The DEFAULT (no designType) is the solid filled variant — that's the primary
button. To get a secondary button, use `_outlined`.

Other Button slots: `label`, `onClick`, `colorScheme`, `leftIcon`, `rightIcon`.

## Catalog gaps (discovered 2026-05-18)

The fallback `ComponentCatalog` only knows 18 components:
`Button, Calendar, CheckBox, Dropdown, Flex, Grid, Icon, Image, Label, Menu,
RadioButton, Stepper, Table, Tabs, Text, TextArea, TextBox, ToggleButton`.

Missing: `Link, ArrayRepeater, Carousel, Popup, Popover, FileUpload,
FileSelector, SchemaForm, Otp, Tags, TextEditor, MarkdownEditor, Chart, Iframe,
Video, Audio, ProgressBar, RangeSlider, ColorPicker, Timer, Animator, Gallery,
ImageWithBrowser, Chat, ...` — and probably more I haven't seen yet.

When the catalog returns `Unknown component type 'X'`, fall back to:
1. **Reading prod**: find a page that uses X, fetch its component definition, mine the property keys.
2. **Source**: nocode-ui/.../src/components/X/X.tsx and X.props.ts often define the typed schema.

Set `MODLIX_CATALOG_URL` to a published CDN catalog if available — the
fallback ships with the package and only covers core components.

## When CSS layout properties APPEAR to work

Some CSS properties DO apply because the component's React layer either uses
them directly (`flexWrap`, `gap`, `padding`) or because they target a leaf
HTML element the component renders (`color`, `fontSize`). The catch is that
`display`, `flex-direction`, `grid-template-columns`, and other CSS layout
properties that conflict with the component's own structural decisions get
silently overridden. Use the component's typed property instead.

## ROADMAP items this surfaced

- Enrich the component catalog with the missing 50+ component types (the
  catalog is loaded at agent startup from the CDN; see
  [`app/agents/appbuilder/catalog.py`](../../catalog.py)).
- Add `inspect_component_in_prod(type)` tool that mines a real prod page for
  property-key examples when the catalog is incomplete.
