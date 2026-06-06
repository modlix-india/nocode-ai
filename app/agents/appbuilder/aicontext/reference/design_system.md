---
name: Modlix design system — theme tokens + per-component property catalog
description: How the platform's design tokens work, the full per-component (property × enum × token) catalog, and the rules for using them instead of inline overrides.
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

| Slot | Role in appbuilder |
|---|---|
| `colorOne` | PRIMARY brand color (amber `#F59E0B`) |
| `colorTwo` | Brand color hover/darker (`#D97706`) |
| `colorThree` | Brand color deepest (`#B45309`) |
| `colorFour` | Ink white `#FFFFFF` |
| `colorFive` | Muted text `rgba(255,255,255,0.70)` |
| `colorSix` | Faint text `rgba(255,255,255,0.55)` |
| `colorSeven` | Faint border `rgba(255,255,255,0.10)` |
| `colorEight` | Page bg `#0a0a0a` |
| `colorNine` | Elevated bg `#131316` |
| `colorTen` | Surface bg `#1A1A20` |
| `colorEleven..Fifteen` | Extra accents (we point them at `<colorOne>` for consistent amber accents) |

### fontColor — text inside components

| Slot | Default |
|---|---|
| `fontColorOne` | `#FFFFFF` — primary text |
| `fontColorTwo` | `rgba(255,255,255,0.70)` — muted |
| `fontColorThree` | `#FFFFFF` — Link `_primary` default |
| `fontColorFour` | `rgba(255,255,255,0.70)` — Link `_secondary` |
| `fontColorFive` | `<colorOne>` — Link `_tertiary` (amber accent) |
| `fontColorSix` | `rgba(255,255,255,0.55)` |
| `fontColorSeven` | `<colorOne>` — Link `_quinary` |
| `fontColorEight` | `rgba(255,255,255,0.45)` |
| `fontColorNine` | `<colorOne>` — Link `_quaternary` |

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

`Grid`, `Iframe`, `Image`, `ImageWithBrowser`, `Page`, `SubPage`, `SectionGrid`, `Form`, `FormEditor`, `Popover`, `Prompt`, `SchemaForm`, `SchemaBuilder`, `MarkdownEditor`, `TextEditor`, `TextList`, `TemplateEditor`, `ThemeEditor`, `Tags`, `Animator`, `Carousel`, `Timer`, `AnalyticsQuery`, `KIRunEditor`, `PageEditor`, `Popup` (mostly), `SSEventListener`, `WebAnalyticsWidget`, `ProductAnalyticsWidget`, `SessionReplayList`, `SessionReplayPlayer`, `FillerDefinitionEditor`, `FillerValueEditor`.

For these, set spacing/layout/visibility properties + use the parent Grid's layout slots. Don't try to set `colorScheme` — they don't have one.

## Working flow

When applying a style to a component:

1. **Look up the component** in the catalog. Note its styling props and what enum values they accept.
2. **Pick the right enum values** matching your design intent (refer to fontColor*/backgroundColor* assignments in the [token vocabulary section](#colors-numbered-semantically-empty-primitives)).
3. **Set those properties** via `patch_component_props` — e.g. `{textColor: "_lightPrimaryText"}` on Text for amber.
4. **For overrides outside the theme system** (layout, animation, positioning), use `patch_component_styles` for inline values.
5. **Strip inline color/font overrides** that were previously set — use `remove_component_styles` (the inverse of `patch_component_styles`) so the theme cascade actually wins.

## Brand-to-theme mapping (appbuilder, 2026-05-18)

```yaml
primaryFont:   "400 14px/1.4 'Geist', system-ui, sans-serif"
secondaryFont: "400 14px/1.4 'Inter', system-ui, sans-serif"
tertiary..senaryFont: <primaryFont>

colorOne..Three:    amber primary / hover-dark / deeper (#F59E0B, #D97706, #B45309)
colorFour:          #FFFFFF
colorFive..Six:     muted whites
colorSeven..Ten:    faint border + dark surfaces
colorEleven..Fifteen: all → <colorOne>

fontColorOne..Two:    white / 0.70 white
fontColorThree..Four: white / 0.70 white (Link default + secondary)
fontColorFive:        <colorOne> (Link _tertiary = amber)
fontColorSix:         0.55 white
fontColorSeven:       <colorOne>
fontColorEight:       0.45 white
fontColorNine:        <colorOne>

backgroundColorOne:     <colorOne> (amber buttons)
backgroundColorTwo..Ten: hover/darker/elevated/page surfaces
backgroundHoverColor*:   amber-dark / faint white hover surfaces
backgroundDarkerColor*:  amber-deep / dark surfaces

bodyBackground: subtle amber-tinted radial + diagonal slate gradient
```

Read live with `get_theme appbuildertheme`.

## How to refresh this doc

```
cd modlix-mcp
python scripts/build_design_system_reference.py > /tmp/catalog.md
# diff against this doc's catalog section, update inline
```

Run this when nocode-ui ships new components or new design-type variants. The script reads each component's source files and the lazy-loaded `dist/styleProperties/<Component>.json` for accuracy.
