---
name: Modlix design platform — how styling resolves
description: App-agnostic: the four style-resolution layers, why a literal on a page forks from the theme and a Theme. expression does not, component variants and when one actually exists, Text roles, what cannot be themed, and the traps. No colours or sizes; read the app's own theme for those.
type: reference
---

# Modlix design platform: how styling actually resolves

<!-- GENERATED FILE. Source: modlix-apps/DESIGN_PLATFORM.md
     Edit the source and run modlix-apps/tools/sync_design_platform.py.
     Edits made here are overwritten. -->

Read this before styling any Modlix page, in any app. It is app-agnostic: it
describes how the platform decides what a component looks like, which decisions
belong in a theme, and the traps that cost real time. **It contains no colours and
no sizes** — those are per app.

Each app keeps its own answers next to this file:

| app | its design doc |
|---|---|
| appbuilder | [appbuilder_SYSTEM/DesignGuidelines.md](appbuilder_SYSTEM/DesignGuidelines.md) |
| others | none yet; §9 says how to derive one |

Derived 2026-08-29 from `nocode-ui/ui-app/client/src`, verified against a live
local stack.

---

## 1. The resolution order

Four layers decide a component's appearance. Highest wins:

1. **Inline `styleProperties` on the page component.** Rendered as a `style`
   attribute, so it beats everything below it, always.
2. **The app's theme**, as a stylesheet rule keyed on the component's classes.
3. **The component's `spv` defaults** — a per-variant default table declared in the
   component's `*StyleProperties.ts`.
4. **App-level variable defaults** in `nocode-ui/ui-app/client/src/App/appStyleProperties.ts`.

Layers 3 and 4 are the ones people forget. A variable that is not in the theme
document is often still defined at layer 4, so "absent from the theme" does not
mean "renders as nothing". `backgroundColorOne` is not in appbuilder's theme at
all; it is declared in `appStyleProperties.ts` as `dv: '<colorOne>'`, which is
where the primary button's fill comes from.

**An unknown variable resolves to the empty string**, not to itself. So
`<neverDefined>` produces `color: ;`, which the browser silently drops. Check what
a default *resolves to*, not just whether the theme names it.

## 2. A literal on a page forks from the theme. A reference does not.

**The `<var>` syntax does not work in a page style leaf.** Writing `<fontColorOne>`
there does nothing: `processStyleValueWithFunction` expands `<var>` for *theme*
values only. Paste a hex instead and you have forked that value from the theme
permanently, with no later reconciliation.

**But a page style leaf CAN reference the theme, through an expression:**

```json
{"text-color": {"location": {"type": "EXPRESSION", "expression": "Theme.fontColorOne"}}}
```

`ThemeExtractor` (`context/ThemeExtractor.ts`, prefix `Theme.`) is registered
alongside `Store`, `LocalStore`, `Page` and `Parent`, and reads the theme out of
`store.theme` with full breakpoint resolution. Verified live: a leaf bound to
`Theme.colorOne` renders `#F59E0B`.

Two caveats. It returns the variable's **raw** value, so a variable whose value is
itself `<anotherVar>` comes back unexpanded; use it for leaf palette tokens. And it
is still a page-side style, so it does not benefit other instances.

So the order of preference is:

1. **Change the theme.** One rule reaches every instance and keeps a re-skin
   possible. This is right whenever you are styling *the component*.
2. **`Theme.` in an expression**, when it genuinely is this one instance, or the
   value is conditional. Stays on-palette.
3. **A literal**, only for something that is genuinely not a design token: a
   layout number, a one-off gradient.

Appbuilder reached 16,112 literal leaves and **zero** theme references before its
values were migrated back. That was a habit, not a constraint.

## 3. What a style property is legitimately for

- **Layout.** `display`, `flexDirection`, `gap`, `width`, `flexGrow`, `alignItems`,
  `justifyContent`, `padding` on a `Grid`. Layout is per-instance by definition.
  In practice this is about 60% of all style leaves and it is all correct.
- **One-off placement.** `marginLeft: auto`, a fixed column width.
- **State the theme cannot express**, via an `EXPRESSION` leaf: the active item in a
  segmented control, a selected row.
- **Truncation and overflow** on a specific container.
- **A genuinely unique surface**: a landing hero, a marketing gradient.

Not for: colour, font size, weight, borders, radius, hover/focus/disabled, or
padding *inside* a standard component. Those are the component's, and the theme's.

## 4. Theme variable naming

Variables inflate as `<component><Property><designType><colorScheme>`, with each
enum value stripped of non-alphanumerics and upper-cased on the first letter
(`removeSpecialCharsAndMakeFirstLetterCap` in `lazyStylePropertyUtil.ts`):

```
_default + _primary   ->  buttonPaddingDefaultPrimary
_text    + _quaternary ->  buttonHoverColorTextQuaternary
SPAN     + _subText    ->  textColorSPANSubText
```

Some properties spray on different axes. `Text` sprays on
`<textContainer><textColor>`, not designType/colorScheme. Read the component's
`*StyleProperties.ts` for the `n:` pattern before guessing a name.

**One key part per placeholder.** A name with two placeholders needs both parts, or
the property emits no CSS and gives no error.

## 5. Variants: design type and colour scheme

Every component inherits `designType` (default `_default`) and `colorScheme`
(default `_primary`) from `COMMON_COMPONENT_PROPERTIES`, and most add their own
design types on top. **Leaving them unset is normal**: unset takes the default, and
the default is usually what you want. Set them only to deviate.

Design types available per component (each also has `_default`):

| component | extra design types |
|---|---|
| Button | `_outlined` `_text` `_iconButton` `_iconPrimaryButton` `_fabButton` `_fabButtonMini` `_decorative` `_bigDesign1` |
| Dropdown | `_outlined` `_filled` `_bigDesign1` `_text` `_editOnReq` |
| TextBox | `_outlined` `_filled` `_bigDesign1` `_editOnReq` |
| CheckBox | `_outlined` `_filled` |
| Link | `_underLine` `_underAboveLine` `_sideLines` |
| Menu | `_outlined` `_text` `_sides` `_topbottom` |
| Popup | `_design1` `_design2` |
| ToggleButton | `_outlined` `_squared` `_bigknob` `_small` |

Colour schemes are always `_primary` `_secondary` `_tertiary` `_quaternary`
`_quinary`.

**A variant only exists if your theme defines variables for it.** Five schemes on
paper does not mean five schemes in your app. Count before you pick:

```
# how many variables back Dropdown's _outlined + _secondary in this theme?
GET /api/ui/themes/{id}  ->  variables.ALL
count keys matching  ^dropdown.*OutlinedSecondary$
```

Zero means the combination falls through to the component's `spv` default, which is
generic and almost never matches your palette. Picking it does not give you a
differently-styled component, it gives you an unstyled one.

## 6. Text is not styled, it is assigned a role

`Text` is usually the most numerous component on a page and the largest source of
style drift. It has a role system that most apps never wire up.

`Text/TextStyleProperties.ts` sprays `textFont<textContainer><textColor>` (a CSS
`font` shorthand) and `textColor<textContainer><textColor>`:

- `textContainer` — 11 values: `SPAN` (default), `H1`..`H6`, `P`, `B`, `I`, `PRE`.
  This is the HTML element, so it is a **semantic** choice. Promote a `SPAN` to a
  heading for display type; never demote an existing heading to make it smaller.
- `textColor` — 13 values: `_primaryText`, `_subText`, `_labelText`,
  `_paragraphText`, four `_light*Text`, and `_coloredText1`..`_coloredText5`.

The variable is keyed on the **pair**, so a role can carry size, weight and colour
together. Treat a role as a complete text style, not just a colour, and you get a
13-slot type system for free.

The component's own `spv` already maps every role to a theme colour
(`_primaryText` → `<fontColorOne>`, `_subText` → `<fontColorTwo>`, and so on), so a
sensible theme needs to define very little before roles start working.

**Setting `textColor` is only half the job.** Inline styles beat the theme, so a
Text keeps rendering its literal font and colour until those leaves are deleted.
Deletion is what makes the theme visible.

## 7. What cannot be themed at all

**`np: true` does not mean "themeable".** It means **no prefix**: `processEachResolution`
emits `propDef.np ? propDef.sel : prefix + ' ' + propDef.sel`. A property with no
`np` is themeable too, just automatically prefixed. Counting `np` to decide what a
component exposes gives an answer that is wrong in both directions.

What a component exposes is the length of the array it hands to
`stylePropertiesForTheme`. Read that. For the twenty-one lazy-loaded components it
lives in `dist/styleProperties/<Name>.json`, which is **hand-maintained and
git-tracked, not build output** — the catalog generator only reads it. For the rest
it is the `stylePropertiesForTheme` export in
`src/components/<C>/<c>StyleProperties.ts`.

Genuinely empty, and therefore genuinely unthemeable:

| component | why |
|---|---|
| SubPage | has no chrome of its own |
| TextEditor | wraps Monaco, which themes itself; its own border is a real gap |

**Grid is the interesting case.** It has ~33 properties, but they are not
`gridBackground` and `gridBorderRadius`; they are a fixed menu — `borderLight`,
`borderRadiusRound`, `boxShadowDarkLow`, `paddingDesignOne` — selected by putting
`_LIGHT` or `_ROUND` on the instance. There is no background variable at all. So an
app that paints cards with Grids cannot theme them, and that is a design-language
gap rather than an oversight: what those apps want is a Card component.

Two more to know before theming:

- **`Icon` has no themeable font size.** Only its colour, border, radius, padding
  and background can move to a theme.
- **The toast container's stacking order is not themeable.** `MessageStyle.tsx`
  hardcodes `z-index: 12` on `.comp.compMessages`. Everything else about the
  container is a variable — `left`, `right`, `top`, `bottom`, `transform`, `margin`
  — so a toast can be moved anywhere except *above* app chrome that outranks 12. A
  fixed header at `z-index: 30` will cover it. Position around the chrome, or add a
  `messagesZIndex` property the way `popupZIndex` was added.
- **`Link` and `Icon` collide.** Link's variables are all named with an `icon`
  prefix, and two of them — `iconBorder<designType><colorScheme>` and
  `iconPadding<designType><colorScheme>` — are declared by *both* components with
  the same CSS property. Setting either one styles every Link **and** every Icon on
  the matching variant. Verified by reading both catalogs; treat those two names as
  unusable until upstream renames Link's.

## 8. Traps that cost real time

- **All base style leaves must live under ONE rule key.** The runtime's
  `createNewState` overwrites `pseudoStates[state].defaultOne` per rule, so two
  unconditioned rules on one component silently lose all but the last.
- **Never delete an `EXPRESSION` leaf when cleaning up inline styling.** It holds
  *state*, not a value. Removing the `text-color` expression from a segmented
  control deletes the active-item highlight and gives no error.
- **A `Text` can be a glyph** — a dot, a chevron, a close cross. Its size is
  functional; snapping it onto a type scale breaks the layout around it.
- **`LUXON_FORMAT` needs epoch SECONDS.** It does `parseInt(value)` then
  `DateTime.fromSeconds(...)`, so an ISO string renders as 01 Jan 1970 because
  `parseInt("2026-08-29T…")` is `2026`. Fix the endpoint, not the page.
- **`update_theme` replaces the entire variable map.** Use `patch_theme_variables`
  for anything short of a wholesale replacement: it reads, applies only your
  `set_variables` / `remove_variables`, writes back, and verifies. `update_theme`
  now refuses a write that would drop existing variables unless `confirm_drop=true`.
  Note what the storage actually does, because it is not what the tool names
  suggest: `read` returns the merged view, and `update` runs `extractOverride`, so
  the server persists only the delta against the parent chain, with `null` marking
  a deletion. Sending the whole merged document IS the designed write shape for
  every overridable type, and `StyleThemeService.updatableEntity` version-checks it
  (412 on mismatch), so a concurrent write is rejected rather than lost.
- **A `TextEditor` inside a `Popup` renders blank.** It measures a zero-size
  container on mount, and `vh` heights do not resolve in an auto-sized modal.
- **`showEmptyRows` is a `TableColumns` property**, not a `Table` property.
- **A `font` shorthand is usually the whole typography lever.** Most components
  declare one `font` variable, not `fontSize` and `fontWeight` separately, and it
  carries weight, size, line-height and family together. So a page writing
  `fontSize: 12px` inline is not hitting a platform gap, it is spelling out the
  shorthand. Move it by rewriting the shorthand, and carry through the parts you
  are not changing: re-emitting `font` **resets everything it omits**, so dropping
  the `/line-height` silently reverts that to normal. Both TextBox and Dropdown
  declare `input { font: inherit }`, so the root shorthand reaches the typed text
  and no separate input-size variable is needed. What the shorthand genuinely
  cannot carry: `letter-spacing`, `text-transform`, `text-decoration`, `color`.
- **The Table family's variable names come from the TABLE, not the column.** Every
  rule is selected as `.comp.compTable<tableDesign><colorScheme> .comp.compTableColumn`,
  so a column's variable is `tableColumnFontSize<tableDesign of its Table>`. Reading
  `tableDesign` off the column finds nothing and falls back to `_default`, naming a
  variable no rule will ever match. The theme write succeeds, the CSS never appears,
  and the only symptom is the styling quietly missing. Cost an hour; found by
  diffing the emitted stylesheet, which listed `._design1` through `._design4` and
  no `._default`.
- **Spending all four `_light*Text` roles leaves you no on-dark text.** They are
  named for on-dark surfaces. An app with almost no dark ground is tempted to
  repurpose them as extra light-ground styles — and then the one tooltip that IS
  dark gets handed a dark grey and disappears. Keep one in reserve, or accept that
  on-dark text needs a `Theme.` expression every time.

- **A hover state can live in the leaf KEY, not in a rule.** A page writes a
  button's hover border as `borderTopColor:hover` inside the ordinary base rule,
  not as a rule with `pseudoState` set. Tooling that filters on `pseudoState`
  silently ignores every one of them.
- **A border spelled as longhands is still a border.** `borderTopStyle`,
  `borderTopWidth` and `borderTopColor` across four sides is one `border`
  shorthand written twelve ways. Any audit that classifies style leaves needs them
  in its appearance set, or a component that states its whole border that way looks
  clean.
- **Adding a design type is often cheaper than fighting one.** If a component has
  no size axis and your app needs two sizes, check whether anything in its
  `*Style.tsx` is actually keyed on the existing design-type class. Button's
  variants are entirely `spv`-driven, so a new one is its enum entry plus a clone of
  the neighbouring spv keys, inert until something selects it. Spending a colour
  scheme on "compact" instead leaves a name nobody can read later.

- **A dialog that hand-builds its own header will drift from every other dialog.**
  `Popup` renders a title row and a close control for you when you set `modelTitle`,
  `showClose` and `designType: _design1`. A page that instead sets
  `showClose: false` and composes a Grid with a Text title and an icon Button gets
  a title at whatever size it typed and a close control shaped like a button, and
  no theme change will ever reach either. Check `showClose` before concluding two
  dialogs are styled differently.
- **An unset variant is not a neutral choice, it is an unstyled one.** A Dropdown
  set to `_outlined` in a theme that defines no `dropdownBorderRadiusOutlined*`
  falls through to the component's own spv default and renders as a pill next to
  8px fields. This is §5 in practice: count the variables before picking a variant,
  and prefer leaving `designType` unset.
- **`GET /api/ui/pages/{id}` intermittently 404s right after a PUT to that page.**
  A cache race, not a missing page. Retry before believing it.

- **A `//` comment containing an apostrophe breaks naive catalog parsing.** "the
  box's corner rounding" opens a string as far as a brace scanner is concerned and
  swallows the rest of the file. CheckBox parsed as 2 entries instead of 13, with no
  error — a short catalog just looks like a component with few properties.

## 9. Deriving the design doc for a new app

The appbuilder tooling in
[appbuilder_SYSTEM/tools/](appbuilder_SYSTEM/tools/) is the worked example. It is
currently hardcoded to `appbuilder`; point `APP_CODE`, `THEME_NAME` and `PAGES` at
your app and it runs unchanged.

1. **`style_audit.py --report`** — every style leaf split into layout, appearance
   on themeable components, and appearance on components that cannot be themed.
   That last number is your platform-gap budget; the middle one is your backlog.
2. **Count theme coverage per (component, design, scheme)**, as in §5. Anything at
   zero is a variant your app does not really have. That table *is* the "which
   design should I use" answer, and it is different for every app.
3. **Cluster the `Text` styles.** If the count of distinct (size, weight, colour)
   combinations is more than about 15, the app has drift rather than a type scale.
   Map the clusters onto the 13 roles.
4. **`style_migrate.py --theme` then `--pages`** — write the roles, then set
   `textColor` and delete the inline leaves. Dry-run first; it is idempotent.
5. **`style_components.py --report`** — the same job for everything that is not
   Text. It inverts each component's catalog (via `catalog.py`) so an inline leaf
   names the variable that governs it, then sorts every variable into four
   verdicts: the theme already says this, every instance agrees, they agree but
   adopting would restyle instances that say nothing, and they disagree. Only the
   first two are safe to automate. Record the rest as decisions with reasons —
   `appbuilder_SYSTEM/style-decisions.json` is the worked example — and feed them
   back with `--apply --adopt`.
6. **`style_lint.py`** — fails when a component carries a role AND a literal that
   overrides it. Run it before any page save.
7. Write the app's own doc with the palette, the type scale, the role table and the
   variant table, and link it from the table at the top of this file.

**Deciding per site, not per variable.** Nine buttons saying `#FFFFFF` and a tenth
saying `rgba(255,255,255,.86)` is not a stalemate: the nine restate the theme and
can go, the tenth is a deliberate translucent overlay and stays. Judging the
variable as a whole keeps all ten. And compare values through a normaliser —
`1px solid rgba(10,10,10,.14)` and `1px solid #0A0A0A24` are the same border, and
without that they read as a conflict that is only a difference of notation.

**Never let a tool write an inline value into the theme on its own.** An early
version of `style_components.py` did, whenever leaf and variable disagreed. Its plan
included `buttonFontDefaultPrimary = 500 11.5px Geist, sans-serif`, taken from two
stray buttons, which would have shrunk every primary button in the app.

Prove it worked by repointing one colour variable in the theme and confirming
everything that should follow, follows. If it does not, the values are still on the
pages.

## 10. Before you save a page

- [ ] Did anything you styled belong in the theme instead?
- [ ] Every `Text` on a role, with no inline font or colour left on it?
- [ ] Where you deviated from the default design or scheme, does the combination
      actually have theme coverage?
- [ ] Layout leaves only: no colour, font, border, radius or hover on a themeable
      component?
- [ ] `EXPRESSION` leaves left intact?
- [ ] Rendered it and looked at it, rather than trusting the definition?
