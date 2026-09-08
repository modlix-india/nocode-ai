# Theme variable naming, and chrome the theme cannot express

Companion to `platform_doc_read('design_system')`. That doc covers the token
vocabulary; this one covers the shapes that silently no-op and the few things
that legitimately belong in the style doc.

## Override variable names are camelCase with the placeholder expanded

Component style property definitions in `dist/styleProperties/<Name>.json` carry
a property name with placeholders:

```
textBoxHeight<designType><colorScheme>
```

At runtime the platform expands each placeholder against the enum values from
`propertiesDefinition`, running every value through
`removeSpecialCharsAndMakeFirstLetterCap()`, which strips leading underscores
and uppercases the first letter. So `_default` becomes `Default`, `_primary`
becomes `Primary`, `_bigDesign1` becomes `BigDesign1`, `_editOnReq` becomes
`EditOnReq`.

```
textBoxHeightDefaultPrimary       correct
textBoxHeight_default_primary     theme.get() never finds this, silently no-ops
```

The lookup in `processStyleValue()` is an exact `theme.get(variable)`. Wrong
shape means empty result means no rule emitted, with no error anywhere.

To enumerate the real variants for a component, read
`nocode-ui/ui-app/client/dist/styleProperties/<Name>.json`. Each entry has `n`
(the placeholder name), `cp` (the CSS property), `sel` (the emitted selector),
`dv` (default value) and `spv` (a `{"_<design>-": "<default>"}` map, where the
trailing `-` wildcards the colorScheme half).

## Font slots must NOT carry a weight

`primaryFont` through `senaryFont` are CSS `font` **shorthand** values and the
platform composes on top of them. `textStyleProperties.ts` maps:

```
H1 -> 'bold <quinaryFont>'     H2 -> '<quinaryFont>'
H3 -> 'bold <secondaryFont>'   H4 -> '<secondaryFont>'
H5 -> 'bold <tertiaryFont>'    H6 -> '<tertiaryFont>'
SPAN/P/PRE -> '<primaryFont>'  B  -> 'bold <primaryFont>'
```

A slot defined as `700 28px/1.15 Geist` expands to `bold 700 28px/1.15 Geist`,
two weights, invalid shorthand, **silently dropped**. The symptom reads as a
different bug entirely: H1 and H3 collapse to inherited body size while H2 and
H4, which take the slot unprefixed, look perfect.

Write slots as `<size>/<line-height> <family>` only. Weight comes from the
platform's `bold` prefix or a per-component override.

Note the pairing when choosing containers: a design's h1/h2/h3 map to Modlix
**H1 / H3 / H5**, not H1/H2/H3.

## `fontColorFive..Eight` are not only text colours

They look like they only drive Text's colour schemes, and they do not. Setting
`fontColorEight` to white for a dark-background text scheme turns **every
TextBox and TextArea border white** across the whole app; the fields render as
bare placeholder text with no box. Verified by probe: `fontColorEight:
rgb(1,2,3)` renders `.comp.compTextBox._default._primary` with
`border: 1px solid rgb(1,2,3)`.

Platform defaults are `fontColorFive: <colorThree>`, `Six: <colorNine>`,
`Seven: <colorFive>`, `Eight: <colorThirteen>`. If nothing uses the `_light*Text`
schemes, do not set Five through Eight at all; deleting them restores the
defaults, which is what form controls want.

Do not reach for `textBoxBorderDefaultPrimary` to work around it. That name is
live, but a CSS `border` shorthand value there renders as `0px none` and wipes
the border entirely.

## Prefer design types and colour schemes over overrides

Every component ships `_primary` through `_quinary` wired to `colorOne` through
`colorFive`, plus several design types. Set the five base colours once and the
schemes become the brand palette, then pick the scheme on the component. Check
what a design type already gives you first: Button `_outlined` is a 16px radius
on a 32px control, already a pill, so a pill outline button needs no theme
change at all.

## Global chrome goes in `body::before`

When a visual element must appear on **every** page (a brand stripe at viewport
top, a footer marker, an accent rail), use a fixed-position `body::before` or
`body::after` in the style doc.

Not the theme: the app's style properties (`bodyBackground`, `bodyMargin`,
`bodyFont`) have no `appBorderTop` or always-on stripe variable. `bodyBackground`
can take a gradient, but most pages set their own page-root background with
`min-height: 100vh` and a solid colour, which covers the body's background
entirely, so the stripe is never visible.

Not per-page `styleProperties`: it works on one page, and disappears the moment
the user routes elsewhere. Keeping 30+ pages in sync is brittle.

```css
body::before {
  content: '';
  position: fixed;
  top: 0; left: 0; right: 0;
  height: 2px;
  background: #F59E0B;          /* sync with theme.colorOne */
  z-index: 9999;
  pointer-events: none;
}
```

`z-index: 9999` is deliberate so the stripe survives modals and popups.
`pointer-events: none` so it never intercepts clicks. The style doc has no token
substitution, so the colour is a literal: leave the sync comment and update both
places. The same recipe gives a bottom stripe (`body::after` with `bottom: 0`),
an accent rail (`top: 0; bottom: 0; left: 0; width: 2px`) or a watermark.

When you add one, delete the per-page `borderTop` rules that were the previous
way of expressing it, so there is one source of truth.

## The scrollbar width trap

`appStyleProperties.ts` exposes the scrollbar width as TWO separate variables:

```
scrollBarWidth        -> ::-webkit-scrollbar          default 7px
scrollBarHoverWidth   -> *:hover::-webkit-scrollbar   default 7px
```

Set only `scrollBarWidth` to 10px and the hover twin stays at 7px, so the bar
**shrinks 3px the moment the pointer touches anything on the page**. If the app
also has a global transition rule, the entire layout then animates. Always set
both to the same value, or neither. The same pairing exists for
`scrollBarThumbBg` / `scrollBarThumbHoverBg`, harmless there because it is only
a colour.

Belt and braces in the style doc, so a scrollbar appearing or disappearing
cannot change the width either:

```css
html { scrollbar-gutter: stable; overflow-y: scroll; }
```

`scrollbar-gutter` covers modern browsers; `overflow-y: scroll` is the Safari
fallback.

## Do not put a global transition in a style doc

A rule like

```css
* { transition: width 1s, height 1s, padding-left 1s, padding-right 1s,
                padding-top 1s, padding-bottom 1s, background-size 1s; }
```

makes every element on every page animate any width, height or padding change
over a full second. Combined with the scrollbar trap above, the whole page
visibly slides. It also makes every hover that changes padding feel sluggish,
which reads as a broken component rather than a global rule. Scope it to the
elements that need it.
