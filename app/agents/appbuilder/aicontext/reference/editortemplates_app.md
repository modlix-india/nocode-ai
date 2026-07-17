---
name: editortemplates app — canonical component variants
description: The editortemplates app is a separate Modlix app whose pages each demonstrate ONE component type with every colorScheme × designType variant. Use it as the canonical reference for what each Modlix component CAN look like, and as a model when building theme-test pages in your own app.
type: reference
---

# editortemplates app

There is a separate Modlix app, **`editortemplates`**, that exists solely to
demonstrate every component the platform exposes. The page editor in
nocode-ui renders these pages in an iframe inside its "Insert component"
menu (see `nocode-ui/ui-app/client/src/components/PageEditor/components/ComponentMenu.tsx:564`,
`src={`/editortemplates/SYSTEM/page/${pageName}`}`).

## What's in it

27 pages, each named after a component type, each showing:
- All 5 platform colorSchemes (`_primary`, `_secondary`, `_tertiary`, `_quaternary`, `_quinary`) — plus a disabled state
- All applicable designTypes for that component (`_outlined`, `_text`, `_iconButton`, `_fabButton`, `_decorative`, etc.)

Page index (as of this writing):

| Page | Component |
|---|---|
| `buttons` | Button (filled / outlined / text / icon / fab / fab-mini / decorative / big-design) |
| `buttonbar` | ButtonBar |
| `textBox` | TextBox (5 colorSchemes, plus "Special TextBoxes" section) |
| `textArea` | TextArea |
| `dropdown` | Dropdown |
| `checkbox` | CheckBox |
| `radioButton` | RadioButton |
| `togglebuttons` | ToggleButton |
| `otp` | Otp |
| `text` | Text (Heading 1–6, normal, eyebrow `MAIN`) |
| `textDecorative` | Text — decorative variants |
| `textParagraph` | Text — paragraph styling |
| `link` | Link (default / underline / below-above line / side lines × 5 colorSchemes) |
| `icon` | Icon |
| `iconButtons` | Icon Button |
| `grid` | Grid |
| `popup` | Popup |
| `popover` | Popover |
| `tab` | Tab |
| `fileupload` | FileUpload |
| `video` | Video |
| `stepper` | Stepper |
| `colorPicker` | ColorPicker |
| `rangeSlider` | RangeSlider |
| `progressBar` | ProgressBar |
| `verticalMenu` / `horizontalMenu` | Menu |

URL pattern: `https://<host>/editortemplates/SYSTEM/page/<pageName>`.
List them via `list_pages(app_code="editortemplates")`.

## What to use them FOR

1. **Visual reference for variants.** Before deciding which `colorScheme` /
   `designType` to set on a component, screenshot the relevant
   editortemplates page to see all options side-by-side.
2. **Reading the platform's intent.** Each page is curated by the platform
   author — the layout shows the *expected* combinations and which ones are
   "real" first-class variants vs decorative edge cases.
3. **Modelling your own theme-test page.** If you're rebuilding a theme for
   a target app, copy the editortemplates page structure into your app and
   render it under YOUR theme to see what's broken.

## What you CAN'T use them for

**The editortemplates pages render in the editortemplates app's own theme,
NOT in your target app's theme.** Switching MODLIX_DEFAULT_APP_CODE doesn't
help — the URL `/editortemplates/SYSTEM/page/X` always loads with the
editortemplates app's theme (an app's theme is bound to its appCode, not the
current request context).

So screenshot the editortemplates pages to see *what the platform supports*,
but to verify *what your appbuilder/<yourapp> theme does* with each variant,
you must build the same component grid as a page in your own app:

```python
# WRONG: this renders with editortemplates theme, useless for diagnosing your theme
screenshot_page(app_code="editortemplates", page_name="buttons")

# RIGHT: build the same component layout as a page in your own app
create_page(name="themetest", app_code="appbuilder")
add_component(... colorScheme="_primary" ...)  # one per scheme
screenshot_page(app_code="appbuilder", page_name="themetest")  # renders with appbuilder theme
```

## Diagnostic workflow when something looks broken

1. **Spot the problem.** Open the affected page; note which component +
   colorScheme is unreadable (e.g. "TextBox label is white-on-light").
2. **Cross-check with editortemplates.** Open the matching editortemplates
   page (e.g. `textBox`) — confirm the platform's intent for that scheme.
3. **Reproduce in YOUR app.** Build (or open) a themetest page in your app
   that uses the same colorScheme. Confirm the issue is theme-driven, not
   inline-style-driven, by checking the styleProperties on the broken
   instance (use `get_component_styles` MCP tool).
4. **Fix the theme token.** The CSS for component+scheme combinations is
   generated from the theme's per-color tokens (`colorOne..Ten`,
   `fontColorOne..Nine`, `backgroundColorOne..Ten`). Update the right token
   in the theme, NOT the per-component inline style.
5. **Re-render the themetest page** to verify the fix applies app-wide.

See [[reference-design-system]] for the full token vocabulary.
