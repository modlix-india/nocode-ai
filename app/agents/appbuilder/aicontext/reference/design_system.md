---
name: Modlix design system — theme tokens + per-component property catalog
description: How Modlix styling resolves (theme, component spv, app defaults), the per-component property x enum x token catalog, Text roles, when a variant actually exists, and the traps. App-agnostic; read the app's own theme for its palette.
type: reference
---

# Modlix design system

The platform has a **built-in design token system**. Every styled component reads colors, fonts, and other design values from a numbered **theme token vocabulary**, picked by enum-valued **styling properties** (most commonly `designType` + `colorScheme`, but some components use other names — see the catalog below).

**Use it.** Set the theme once, set the right enum properties on each component. Don't hardcode `color: "#F59E0B"` or `fontFamily: "'Geist', sans-serif"` inline — those reads pay no rent.

## The two-layer model

1. **Theme** (`appbuildertheme.variables.ALL`) maps numbered slot names (`colorOne..Ten`, `fontColorOne..Nine`, `backgroundColorOne..Ten`, `primaryFont`, `errorColor`, etc.) to concrete values.
2. **Components** are wired to read those slots — selected by enum properties on each instance. The same component renders differently depending on which `(designType, colorScheme)` (or `(textContainer, textColor)`, etc.) you pick.

Changing the theme cascades through every page. Setting the right enum on each instance is how you opt into the brand.

## How property-name conventions vary

| Convention | Examples |
|---|---|
| `designType` + `colorScheme` | Button, Link, TextBox, CheckBox, RadioButton, Dropdown, Calendar, Tabs, etc. (most components) |
| `textContainer` + `textColor` | Text (the property that picks SPAN/H1/...P/PRE is `textContainer`; the color is `textColor`) |
| `designType` only | ArrayRepeater, Popup, etc. |
| `colorScheme` only | RangeSlider, Stepper, etc. |
| No styling props | Grid, Iframe, Image (pure-layout / pre-styled containers) |

Verify the right property name via the [component catalog](#component-catalog) below before writing patches. Setting `colorScheme=_lightPrimaryText` on Text does nothing — Text doesn't have a `colorScheme` property. Use `textColor=_lightPrimaryText` instead.

## Token vocabulary

These are the slot names components reference via `<varname>` in their `dv` (default value) and `spv` (style-property-value-per-enum-combo) fields. Set them in `appbuildertheme.variables.ALL`.

### Fonts
**MUST be a valid CSS `font` shorthand**, not just a family list. The platform's `appStyleProperties.ts` and component style rules use `cp: 'font'` (the shorthand), which requires at minimum `<font-size> <font-family>`.

```
BAD  → "'Geist', system-ui, sans-serif"            (browser strikes the rule)
GOOD → "400 14px/1.4 'Geist', system-ui, sans-serif"
```

The baseline size (`14px`) doesn't lock in the value — per-component `font-size` overrides still cascade through correctly. The H1 at `font-size: 96px` stays 96px.

| Slot | Typical use |
|---|---|
| `primaryFont`   | Main body + headings |
| `secondaryFont` | Secondary variant |
| `tertiaryFont`, `quaternaryFont`, `quinaryFont`, `senaryFont` | Other variants |

### Colors (numbered, semantically empty primitives)

`colorOne..Fifteen`, `fontColorOne..Nine`, `backgroundColorOne..Ten`,
`backgroundHoverColorOne..Five`, `backgroundDarkerColorOne..Five`.

**The slots carry no fixed meaning and differ per app.** Do not assume what any
number holds. Read the live values before styling anything:

```
get_theme <appTheme>      # or GET /api/ui/themes/{id} -> variables.ALL
```

An earlier version of this doc pinned appbuilder's palette here as a dark theme
(`fontColorOne: #FFFFFF`, page background `#0a0a0a`). Appbuilder has been light
amber-on-near-black-text since, so those values were wrong for three months. Hence:
read the theme, never this table.

**Absent from the theme does not mean absent at render time.** Every one of these
slots also has a default in `nocode-ui/ui-app/client/src/App/appStyleProperties.ts`
(`backgroundColorOne` is `dv: '<colorOne>'`, `fontColorNine` is `<colorFour>`, and
so on), and the runtime merges those in before resolving. A theme that never names
`backgroundColorOne` still renders primary buttons in `colorOne`. Check what a
default *resolves to* before concluding a variable produces nothing.

An **unknown** variable resolves to the empty string, so `<neverDefined>` emits
`color: ;` and the browser drops it. Silent, no error.

### Backgrounds + hover/darker variants

| Slot family | Use |
|---|---|
| `backgroundColorOne..Ten` | Solid bg slots — `One`=amber (primary buttons), `Nine`=page bg |
| `backgroundHoverColorOne..Five` | Hover bg for buttons |
| `backgroundDarkerColorOne..Five` | Pressed / deeper variants |

### Semantic tokens

`errorColor`, `successColor`, `warningColor`, `informationColor`, `mainFontColor`, `lightFontColor`, `textColor`, `gradientColorOne`, `borderColorSix`, plus `bodyBackground` (drives `body { background: ... }`).

### Cross-references

Modlix theme supports `<token>` substitution INSIDE token values, so you can build a semantic layer on primitives:

```json
{
  "colorOne": "#F59E0B",
  "warningColor": "<colorOne>",
  "backgroundColorOne": "<colorOne>",
  "backgroundHoverColorOne": "<colorTwo>"
}
```

## When inline IS appropriate

- **Layout values**: padding, margin, gap, width, position, transform, display, grid-template-* — these are per-instance composition, not design tokens.
- **Animation properties**: animationName, animationDuration, animationDelay, animationFillMode, etc.
- **Hyper-specific overrides**: when a theme slot doesn't exist and adding one isn't justified. Rare. Usually means a missing slot.

Inline is NOT appropriate for:
- `color` / `background-color` / `border-color` → set theme slot via enum property
- `font-family` → use `primaryFont` etc. via theme
- `font-size` / `line-height` that fits a scale → consider adding to theme

## Style-doc substitution gotcha

The Style doc (`appbuilderstyle`) is RAW CSS. It does NOT get `<token>` substitution. If you write `background: <colorOne>` there, you get the literal string. Use hardcoded hex values in the Style doc and comment which theme tokens they mirror; mirror changes when shifting the theme.

## Component catalog

This table is generated by `scripts/build_design_system_reference.py` from nocode-ui's `*Properties.ts` + `*StyleProperties.ts` files and `dist/styleProperties/<Component>.json`. Regenerate when nocode-ui ships new components or new design-types.

Read row-by-row: each component lists every styling enum property + its valid values; sub-components you can target via `sub_component` in `patch_component_styles`; and the theme tokens its rules consume.

### Reading the table

- **Styling props** — set these on the component to pick which token slots fill its styling. `**`prop`**: `_x`, `_y`` means `prop` accepts those enum values.
- **Sub-components** — inner stylable parts (e.g. `image`, `label`, `thumb`). Pass via `sub_component="image"` in `patch_component_styles` to target sub-component-specific rules.
- **Tokens used** — every theme slot the component's rules consume. Make sure these are populated in the theme.

| Component | Styling props (with enum values) | Sub-components | Tokens used |
|---|---|---|---|
| **ArrayRepeater** | _(none)_ | _(none)_ | mainFontColor |
| **Audio** | _(none)_ | `active`, `playBackSpeed`, `playBackSpeedGrid`, `singleSelect`, `speedOption`, `time`, `volumeControls`, `volumeSliderContainer` | primaryFont · backgroundColorFive, backgroundColorFour, backgroundColorOne, backgroundColorThree, backgroundColorTwo · backgroundDarkerColorFive, backgroundDarkerColorFour, backgroundDarkerColorOne, backgroundDarkerColorThree, backgroundDarkerColorTwo · backgroundHoverColorFive, backgroundHoverColorFour, backgroundHoverColorOne, backgroundHoverColorThree, backgroundHoverColorTwo |
| **Button** | **`colorScheme`**: `_primary`, `_secondary`, `_tertiary`, `_quaternary`, `_quinary`<br>**`designType`**: `_default`, `_outlined`, `_text`, `_iconButton`, `_iconPrimaryButton`, `_fabButton`, `_fabButtonMini`, `_decorative`, `_bigDesign1` | `icon`, `leftButtonActiveImage`, `leftButtonIcon`, `leftButtonImage`, `rightButtonActiveImage`, `rightButtonIcon`, `rightButtonImage`, `withLeftIcon`, `withRightIcon` | primaryFont · backgroundColor* (One-Five, Nine) · backgroundDarkerColor* (One-Five) · backgroundHoverColor* (One-Five) · fontColor* (One-Nine) |
| **Buttonbar** | **`buttonBarDesign`**: enum (see properties.ts) | `button`, `first`, `firstChild`, `lastChild`, `selected` | backgroundColor* · backgroundHoverColor* · fontColor* · colorScheme · buttonBarDesign |
| **Calendar** | **`colorScheme`**: `_primary`..`_quinary`<br>**`designType`**: `_default`, `_outlined`, `_filled`, `_bigDesign1`, `_text`, `_editOnReq` | many (date/dropdown/header parts) | primaryFont, quaternaryFont, secondaryFont · backgroundColor* · backgroundHoverColor* · fontColor* · errorColor, successColor · colorNine, colorTwelve · borderColorSix |
| **CheckBox** | **`colorScheme`**: `_primary`..`_quinary`<br>**`designType`**: `_default`, `_outlined`, `_filled` | `checked`, `disabled`, `thumb` | primaryFont · backgroundColor* · fontColor* · colorScheme · designType |
| **ColorPicker** | **`colorScheme`**: `_primary`..`_quinary`<br>**`designType`**: `_default`, `_outlined`, `_filled`, `_bigDesign1`, `_text`, `_boxRoundedDesign`, `_boxSquareDesign` | dropdown/label/icon parts | primaryFont, quaternaryFont · backgroundColor* · fontColor* · errorColor, successColor · colorTwelve |
| **Dropdown** | **`colorScheme`**: `_primary`..`_quinary`<br>**`designType`**: `_default`, `_outlined`, `_filled`, `_bigDesign1`, `_text`, `_editOnReq` | many | primaryFont, quaternaryFont · backgroundColor* · backgroundHoverColor* · fontColor* · errorColor, successColor · colorTwelve |
| **FileUpload** | _(none)_ | many | backgroundColor* · backgroundHoverColor* · fontColor* · borderColorSix |
| **Gallery** | _(none)_ | many | lightFontColor |
| **Icon** | **`colorScheme`**: `_primary`..`_quinary`, `_defaultIcon`, `_lightIcon`<br>**`designType`**: `_default`, `_outlined`, `_filled`, `_rounded` | _(none)_ | backgroundColor* · fontColor* · colorScheme · designType |
| **Jot** | _(none)_ | `default`, `defaultJot`, `filled`, `lightJot`, `outlined`, `primary`, `quaternary`, `quinary`, `rounded`, `secondary`, `tertiary` | backgroundColor* · fontColor* |
| **Link** | **`colorScheme`**: `_primary`..`_quinary`<br>**`designType`**: `_default`, `_underLine`, `_underAboveLine`, `_sideLines` | `showLines`, `sideLines` | primaryFont · fontColor* · colorScheme · designType |
| **MarkdownTOC** | _(none)_ | many | primaryFont · fontColor* |
| **Menu** | _(none)_ | `disabled`, `icon`, `isActive`, `quaternary`, `quinary`, `secondary`, `tertiary` | primaryFont · backgroundColor* · fontColor* |
| **Otp** | **`colorScheme`**: `_primary`..`_quinary`<br>**`designType`**: `_default`, `_dashed`, `_round`, `_filled` | _(none)_ | primaryFont, quaternaryFont · backgroundColor* · fontColor* · errorColor · colorScheme, colorSix · designType |
| **PhoneNumber** | **`colorScheme`**: `_primary`..`_quinary`<br>**`designType`**: `_default`, `_outlined`, `_filled`, `_bigDesign1`, `_editOnReq` | many | primaryFont, quaternaryFont · backgroundColor* · fontColor* · errorColor, successColor · colorTwelve |
| **Popup** | **`designType`**: `_default`, `_design1`, `_design2` | _(none)_ | tertiaryFont · fontColorOne |
| **ProgressBar** | _(none)_ | many | backgroundColor* · fontColor* |
| **RadioButton** | **`colorScheme`**: `_primary`..`_quinary`<br>**`designType`**: `_default`, `_outlined` | `checked`, `disabled`, `selected`, `thumb` | primaryFont · backgroundColor* (incl Eight) · fontColor* · colorScheme · designType |
| **RangeSlider** | **`colorScheme`**: `_primary`..`_quinary` | many | fixedTooltipRangeSliderFont, labelRangeSliderFont, primaryFont, quaternaryFont, senaryFont, tooltipRangeSliderFont · backgroundColor* · backgroundHoverColor* · fontColorOne, fontColorTwo · colorNine |
| **SmallCarousel** | **`colorScheme`**: `_primary`..`_quinary`<br>**`designType`**: `_default` | _(none)_ | _(none — no theme rules; visuals from CSS) |
| **Stepper** | **`colorScheme`**: `_primary`..`_quinary` | many | primaryFont, stepperFont · backgroundColor* · backgroundHoverColor* · colorSeven, colorSix, colorThirteen |
| **Tabs** | **`colorScheme`**: `_primary`..`_quinary`<br>**`designType`**: `_default`, `_line`, `_highlight`, `_underLine` | `active`, `horizontal`, `line`, `underLine`, `vertical` | primaryFont · backgroundColor* · fontColor* · colorScheme · borderColorSix, designType, tabChildContainerBackground |
| **Text** | **`designType`**: `_default`<br>**`textColor`**: `_primaryText`, `_subText`, `_labelText`, `_paragraphText`, `_lightPrimaryText`, `_lightSubText`, `_lightLabelText`, `_lightParagraphText`, `_coloredText1`, `_coloredText2`, `_coloredText3`, `_coloredText4`, `_coloredText5`<br>**`textContainer`**: `SPAN`, `H1`, `H2`, `H3`, `H4`, `H5`, `H6`, `I`, `P`, `B`, `PRE` | `h1`, `h2`, `h3`, `h4`, `h5`, `h6`, `links`, `markdown`, `p`, `textContainer`, `textMarkdown` | primaryFont, quinaryFont, secondaryFont, tertiaryFont · fontColor* (Three-Eight) · textColor · colorEleven, colorFifteen, colorFourteen, colorTen, colorTwelve |
| **TextArea** | **`colorScheme`**: `_primary`..`_quinary`<br>**`designType`**: `_default`, `_outlined`, `_filled`, `_editOnReq` | many | primaryFont, quaternaryFont · backgroundColor* · fontColor* · errorColor, successColor · colorSeven, colorTwelve |
| **TextBox** | **`colorScheme`**: `_primary`..`_quinary`<br>**`designType`**: `_default`, `_outlined`, `_filled`, `_bigDesign1`, `_editOnReq` | many | primaryFont, quaternaryFont · backgroundColor* · fontColor* · errorColor, successColor · colorTwelve |
| **ToggleButton** | **`colorScheme`**: `_primary`..`_quinary`, `_gradient1`<br>**`designType`**: `_default`, `_outlined`, `_squared`, `_bigknob`, `_small` | many | backgroundColor* · fontColorEight, fontColorTwo · bigKnobToggleKnobHeight, defaultToggleKnobHeight, gradientColorOne, outlinedToggleKnobHeight, smallToggleKnobHeight, squaredToggleKnobHeight |
| **Video** | **`colorScheme`**: `_primary`..`_quinary`<br>**`designType`**: `_default`, `_videoDesign1..4` | many | backgroundColor* · fontColorTwo |

> `*` is a shorthand for the slot family (e.g. `fontColor*` = `fontColorOne..Nine`). Full enumeration: run `scripts/build_design_system_reference.py`.

### Pure-structural components (no theme-driven styling)

These render layout containers / non-visual logic — their visuals come from layout properties + your inline composition, not from theme tokens:

`Grid`, `Iframe`, `Image`, `ImageWithBrowser`, `Page`, `SubPage`, `SectionGrid`, `Form`, `FormEditor`, `Popover`, `SchemaForm`, `MarkdownEditor`, `TextEditor`, `TextList`, `TemplateEditor`, `ThemeEditor`, `Tags`, `Animator`, `Carousel`, `Timer`, `AnalyticsQuery`, `KIRunEditor`, `PageEditor`, `Popup` (mostly), `SSEventListener`, `WebAnalyticsWidget`, `ProductAnalyticsWidget`, `SessionReplayList`, `SessionReplayPlayer`, `FillerDefinitionEditor`, `FillerValueEditor`.

For these, set spacing/layout/visibility properties + use the parent Grid's layout slots. Don't try to set `colorScheme` — they don't have one.

`Prompt` and `SchemaBuilder` are NOT in that list: they have no `colorScheme`, but
they do read named theme variables (`prompt*`, `schemaBuilder*`). Set those in the
theme rather than styling them inline.

## A variant only exists if the theme defines it

Five colour schemes on paper is not five schemes in an app. `_secondary` on a
Dropdown is not a different-looking Dropdown, it is an **unstyled** one: with no
matching variables the combination falls through to the component's generic `spv`
default, which almost never matches the brand.

Count before you pick:

```
get_theme <appTheme>  ->  variables.ALL
count keys matching   ^dropdown.*OutlinedSecondary$      # 0 means don't use it
```

In appbuilder, for everything except Button only `_primary` has coverage; Button
additionally has `_quaternary` for delete. Every other app will differ. This is the
single most useful thing to check before choosing a `designType` / `colorScheme`.

**Unset takes the default** (`designType: _default`, `colorScheme: _primary`), which
is usually what you want. Leaving them off is normal; set them only to deviate.

## Text: a role is a whole style, not a colour

`Text` is usually the most numerous component on a page and the largest source of
drift. Its variables are keyed on the **pair** `(textContainer, textColor)`:

```
textFont<textContainer><textColor>    # a CSS font shorthand: weight, size, family
textColor<textContainer><textColor>   # the colour
```

Because the font is keyed on the pair too, a role can carry **size and weight as
well as colour**. Eleven containers times thirteen roles is a type system already
built into the platform, and most apps never wire it up.

- `textContainer` is the HTML element (`SPAN` default, `H1`..`H6`, `P`, `B`, `I`,
  `PRE`), so it is a **semantic** choice. Promote a `SPAN` to a heading for display
  type; never demote an existing heading just to make it smaller.
- `textColor` picks the role. Its `spv` already maps each one to a theme colour
  (`_primaryText` → `<fontColorOne>`, `_subText` → `<fontColorTwo>`, …), so very
  little theme work is needed before roles start working.

**Setting `textColor` is only half the job.** Inline styles beat the theme, so the
Text keeps rendering its literal font and colour until those leaves are deleted.
Use `remove_component_styles` for `fontSize`, `color`, `fontWeight`, `fontFamily`,
`lineHeight` and their `text-` prefixed twins. Deletion is what makes the theme
visible.

## A page CAN reference the theme

The `<var>` syntax does **not** work in a page style leaf: `<fontColorOne>` there
does nothing, because substitution runs on *theme* values only. But an expression
does work:

```json
{"text-color": {"location": {"type": "EXPRESSION", "expression": "Theme.fontColorOne"}}}
```

`ThemeExtractor` (prefix `Theme.`) is registered alongside `Store`, `LocalStore`,
`Page` and `Parent`. Verified live: a leaf bound to `Theme.colorOne` rendered
`#F59E0B`. It returns the variable's **raw** value, so one whose value is itself
`<anotherVar>` comes back unexpanded; use it for leaf palette tokens.

Order of preference:

1. **Change the theme** whenever you are styling *the component*. One rule reaches
   every instance and keeps a re-skin possible.
2. **`Theme.` in an expression** when it really is this one instance, or the value
   is conditional. Stays on-palette.
3. **A literal** only for something that is not a design token: a layout number, a
   one-off gradient.

## Traps

- **All base style leaves must live under ONE rule key.** The runtime's
  `createNewState` overwrites `pseudoStates[state].defaultOne` per rule, so two
  unconditioned rules on one component silently lose all but the last.
- **Never delete an `EXPRESSION` leaf while stripping inline styling.** It holds
  *state*, not a value. Removing the `text-color` expression from a segmented
  control deletes the active-item highlight, with no error.
- **A `Text` can be a glyph** — a dot, a chevron, a close cross. Its size is
  functional; snapping it onto a type scale breaks the layout around it.
- **`LUXON_FORMAT` needs epoch SECONDS.** It does `parseInt(value)` then
  `DateTime.fromSeconds(...)`, so an ISO string renders as 01 Jan 1970 because
  `parseInt("2026-08-29T…")` is `2026`. Fix the endpoint, not the page.
- **`update_theme` replaces the ENTIRE variable map.** For a one-variable change,
  `PUT /api/ui/themes/{id}` with the fetched document. After any theme write, check
  the variable count moved by exactly what you added and spot-check an unrelated
  group: a bad replacement drops whole groups silently.
- **A `TextEditor` inside a `Popup` renders blank.** It measures a zero-size
  container on mount, and `vh` heights do not resolve in an auto-sized modal.
- **`showEmptyRows` is a `TableColumns` property**, not a `Table` property.

## Working flow

When applying a style to a component:

1. **Look up the component** in the catalog. Note its styling props and what enum values they accept.
2. **Pick the right enum values** matching your design intent (refer to fontColor*/backgroundColor* assignments in the [token vocabulary section](#colors-numbered-semantically-empty-primitives)).
3. **Set those properties** via `patch_component_props` — e.g. `{textColor: "_lightPrimaryText"}` on Text for amber.
4. **For overrides outside the theme system** (layout, animation, positioning), use `patch_component_styles` for inline values.
5. **Strip inline color/font overrides** that were previously set — use `remove_component_styles` (the inverse of `patch_component_styles`) so the theme cascade actually wins.

## The app's own palette

Per-app, and not recorded here on purpose — it goes stale and this doc serves every
app. Read it live with `get_theme <appTheme>`, and keep the app's design decisions
in its own folder in the `modlix-apps` repo. `appbuilder_SYSTEM/DesignGuidelines.md`
is the worked example: palette, type scale, Text roles, and which
(design, scheme) combinations that app actually has.

## How to refresh this doc

Re-run the build script that produced the catalog section, then PR the
diff against this doc. The script reads each component's source files
and the lazy-loaded `dist/styleProperties/<Component>.json` for accuracy.

The build script lives in the modlix-mcp archive (retired repo) at
`scripts/build_design_system_reference.py`. If component drift in
nocode-ui makes this catalog stale and the script needs to run again,
port the script into `nocode-ai/scripts/` rather than reviving the
modlix-mcp checkout. Run when nocode-ui ships new components or new
design-type variants.
