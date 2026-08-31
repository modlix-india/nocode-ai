---
name: build-themetest-page
description: Build a single diagnostic page in a Modlix app that renders every component variant (Button × colorScheme × designType, TextBox, Link, Text colors, form controls) under that app's theme — so theme problems become visible at a glance. Use when restyling a theme, migrating between themes, or chasing white-on-white / unreadable component reports.
---

# build-themetest-page

When users say "the TextBox labels are invisible", "the popup buttons can't be
read", "theme is off after dark→light flip", "what designType+colorScheme
should I use for X", or "I want to test theme changes" — build a single
themetest page in their target app, screenshot it, and use the rendering to
diagnose which theme tokens are wrong. **One page covers every component.**

## When to use this skill

- After flipping a theme (dark → light, light → dark, recolor)
- Before/after editing `appbuildertheme` / app-specific theme variables
- When the user reports a specific component looking wrong on a specific page —
  build the themetest to confirm whether it's a theme issue or a page-specific
  inline-style issue
- When a new app is bootstrapped and the theme hasn't been audited yet

## When NOT to use

- For one-off page edits where the user wants to ship a specific component
  styling — patch the page, don't build a diagnostic
- When the user only wants typography/copy changes (no theme touch)
- If a themetest page already exists in the target app — REUSE it (just
  screenshot, don't rebuild)

## What to know first

There IS a `editortemplates` app (separate Modlix app) with one page per
component type showing every variant. **Those pages render with
editortemplates' OWN theme**, not your target app's theme. They're useful
for:
- Visual reference of what each component CAN look like
- Layout patterns to copy into your themetest page

But they CAN'T tell you what's broken in your app's theme — for that you must
build the same component grid as a page IN YOUR APP. See
[[reference-editortemplates-app]].

## What the page should contain

**Minimum diagnostic set:**

| Section | Why |
|---|---|
| **Buttons — filled (default designType)** × 5 colorSchemes | Reveals bg + text-color tokens for primary actions |
| **Buttons — outlined** × 5 colorSchemes | Reveals border + text color tokens on transparent bg (catches white-on-white bugs) |
| **TextBoxes** × 5 colorSchemes, with `noFloat: true` + a Store binding | Reveals label color, placeholder color, border color per scheme |
| **TextBoxes — bound with sample data** | Reveals value text color separately from placeholder color |
| **Links** × 5 colorSchemes × 4 designTypes (`_default`, `_underLine`, `_underAboveLine`, `_sideLines`) | Reveals link color + underline color tokens; surfaces if hover is theme-driven |
| **Text** × every `textColor` enum value | Reveals which text-color tokens have collapsed to the same color, which are unreadable on page bg |
| **Form controls** (CheckBox, RadioButton, ToggleButton, Dropdown) | Reveals check/toggle/dropdown chrome colors |

**For hover states:** screenshot tools can't trigger hover. Either inspect
`appbuilderstyle` for hover rules manually, or extend the screenshot tool to
`page.hover(selector)` before snapping.

## Recommended page structure

```
root (Grid, padding 48px, gap 16px, flex column)
├── title (Text, "Theme test", textContainer=H1, textColor=_primaryText)
├── btnHeader (Text, H3)
├── btnRow (Grid ROWLAYOUT, gap 12px)
│   ├── btnPrimary (Button, colorScheme=_primary)
│   ├── btnSecondary (Button, colorScheme=_secondary)
│   ├── btnTertiary (Button, colorScheme=_tertiary)
│   ├── btnQuaternary (Button, colorScheme=_quaternary)
│   └── btnQuinary (Button, colorScheme=_quinary)
├── btnOutHeader (Text, H3)
├── btnOutRow (Grid ROWLAYOUT, gap 12px)
│   └── ... 5 Buttons with designType=_outlined ...
├── tbHeader (Text, H3)
├── tbCol (Grid, gap 32px, flex column)
│   ├── tbPrimary (TextBox, colorScheme=_primary, noFloat=true,
│   │              bindingPath=Store.themetest.tbPrimary)
│   └── ... 4 more, one per colorScheme ...
├── linkHeader / linkRow / 5 Links (default)
├── linkUlHeader / linkUlRow / 5 Links (designType=_underLine)
├── txtHeader (Text, H3)
├── txtCol (Grid, gap 8px, flex column)
│   ├── tx_primary (textColor=_primaryText, text="_primaryText sample")
│   ├── tx_sub (textColor=_subText)
│   ├── tx_label (textColor=_labelText)
│   ├── tx_paragraph (textColor=_paragraphText)
│   ├── tx_lprimary (textColor=_lightPrimaryText)
│   ├── tx_lsub (textColor=_lightSubText)
│   ├── tx_llabel (textColor=_lightLabelText)
│   ├── tx_lparagraph (textColor=_lightParagraphText)
│   └── tx_c1..tx_c5 (textColor=_coloredText1.._coloredText5)
└── formRow (Grid ROWLAYOUT, gap 24px, alignItems=center)
    ├── cbPrimary (CheckBox)
    ├── rbPrimary (RadioButton)
    └── tgPrimary (ToggleButton)
```

## TextBox quirks worth knowing

- **`noFloat: true`** — without this, the label is positioned absolutely
  ON TOP of the input area, overlapping the placeholder. The label appears to
  "collide" with the placeholder. Always set `noFloat: true` on the test page
  TextBoxes so the label sits above and you can see its color independently.
- **`noFloat` is a real property but the CDN catalog often doesn't list it.**
  It's been added to `PLATFORM_SAFE_PROPS` so `validate_properties` allows
  it; if you see "Unknown property 'noFloat'", restart the MCP to pick up the
  allow-list update.
- **Sample data for "with data" variant:** TextBox doesn't have a
  `defaultValue` prop. To show the bound-with-data state, either type into
  the input pre-screenshot (manual), or add a small `onLoad` event function
  that calls `UIEngine.SetStore` to seed `Store.themetest.<key>` paths. For
  the diagnostic, even empty-bound TextBoxes reveal label/placeholder/border
  colors.

## Building it (sequence)

1. **Create page:** `create_page(name="themetest", app_code="<yourapp>")`.
2. **Add top-level sections** as children of `root`, sequentially (each
   `add_component` does a full-page PUT — parallelism causes optimistic-lock
   conflicts on the page version). Use semantic keys (`btnRow`, `tbCol`,
   etc.) so you can patch them later.
3. **Add components inside each section,** sequentially per section.
4. **Apply layout polish** via `patch_component_styles`: `gap` between TextBox
   rows (32px+ so floating labels don't collide), `padding` on root, `gap`
   between section rows.
5. **Screenshot** with `screenshot_page(full_page=True)`.
6. **Diagnose** which components are unreadable. Common failure modes:
   - Outlined `_primary` invisible → `fontColorThree` or its outlined-text
     token resolves to a near-page-bg color
   - All non-primary outlined buttons identical color → multiple colorSchemes
     collapsed to the same token (e.g., `colorEleven..Fifteen` all =
     `colorOne`)
   - TextBox label same color as placeholder → label-color token wrong for
     scheme
7. **Fix theme tokens** in `appbuildertheme` (or your app's theme). NOT
   inline. The whole point of the test page is to surface theme bugs so
   one token fix benefits every page.
8. **Re-screenshot** the themetest to verify; then re-screenshot the
   originally-broken pages.

## Render context gotcha (Modlix-specific)

When you screenshot `/<appCode>/<clientCode>/page/<pageName>` and the result
shows a sidebar with editor icons + a dotted background + cyan rulers on the
right, you're seeing the **page editor's design surface**, not the runtime
render. The themetest content renders inside a constrained container and
`full_page=true` may capture only the editor's viewport, not the page's
scroll height.

Workarounds:
- Try `anonymous=true` first — if the page is publicly accessible, it'll
  render at runtime without the editor.
- If the page requires auth, render with a non-builder identity (pass
  `username` + `password` for an end-user).
- Or accept the partial visibility and scroll-inspect manually for sections
  below the fold; the visible sections (Buttons + first few TextBoxes)
  usually surface the worst theme bugs anyway.

## Reusability

Once built, KEEP the themetest page in the app. It's tiny (~30 components,
no event functions) and serves every future theme change. Don't delete it
between sessions.

For a new app, copy the page structure from an existing app's themetest:
fetch the page JSON, scrub IDs, POST as a new page in the new app. (Modlix
doesn't have a built-in copy-page-across-apps tool yet — that's a candidate
future MCP tool.)
