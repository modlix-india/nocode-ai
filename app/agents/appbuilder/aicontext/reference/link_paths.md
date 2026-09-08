---
name: Link / linkPath convention
description: Internal links use just /<pageName>; never the full /<appCode>/<clientCode>/page/<pageName> preview path.
type: reference
---

# linkPath convention

## Which components have `linkPath`

NOT every component does. Confirmed via `get_component_schema`:

| Component | Has `linkPath`? | Notes |
|---|---|---|
| `Link` | YES | The canonical clickable text component. |
| `Grid` | YES | Wrap content in a clickable Grid to make any subtree a router link. |
| `Image` | **NO** | Image has only `src`, `alt`, `onClick`. Setting `linkPath` on an Image is silently ignored by the renderer. |

Need to make a non-Link, non-Grid component clickable? Two options (see [§ Making non-linkable components clickable](#making-non-linkable-components-clickable) below).

## Path shape

Anywhere a component takes a navigation target (`Link.linkPath`, `Grid.linkPath`), the value must be **one of three shapes**:

| Shape | When | Example |
|---|---|---|
| `/` | "Take me to the app's default page" — whatever the app currently points home to | Logo click, "Back to home" CTAs |
| `/<pageName>` | Linking to a SPECIFIC page inside the same app | `/showcase`, `/docs`, `/about` |
| Full absolute URL | Linking out to a different host or a different app | `https://cxlanding.modlix.com/`, `https://github.com/modlix-india` |

## "/" vs "/<defaultPageName>"

Use `/` for any "go home" affordance. Confirmed 2026-05-18: hardcoding `/homeTwo` (or whatever the current default page is named) means the link breaks the day someone swaps the default page in the app config. `/` resolves to whatever the app's default page is at request time, so the link survives renames and default-page swaps.

Cases where `/` is the right answer:
- Top-bar logo click
- "Back to home" CTAs (e.g. on a 404, error, or coming-soon page)
- Footer brand wordmark
- Any "exit this flow" button that should land the user on the canonical start page

Cases where `/<pageName>` is the right answer:
- Nav links to specific pages ("Docs", "Showcase", "About") — these mean *that page*, not "the default"
- Footer links to privacy, terms, etc.
- CTAs that explicitly send you to a known destination ("Browse showcase")

## The mistake to avoid

Do NOT use the full preview path that you see in the browser address bar.

```
WRONG:  /appbuilder/SYSTEM/page/showcase
RIGHT:  /showcase
```

The preview URL `https://<host>/<appCode>/<clientCode>/page/<pageName>` (see [Preview URLs](reference_preview_urls.md)) is how the platform serves the page. The router strips the `<appCode>/<clientCode>/page/` prefix before matching against `linkPath`, so a Link with `linkPath="/appbuilder/SYSTEM/page/showcase"` resolves to `/appbuilder/SYSTEM/page/appbuilder/SYSTEM/page/showcase` and 404s.

Use the short form even though it looks "incomplete". The runtime fills in the app + client context automatically.

## Why this is easy to get wrong

The natural mental model is: "the URL of the showcase page is `/appbuilder/SYSTEM/page/showcase`, so to link to it I should use that string." That model is wrong because `linkPath` is **relative to the app's internal routing**, not the gateway's hostname routing.

A useful way to think about it: pretend each app is its own SPA mounted at `/<appCode>/<clientCode>/page/`. Inside the SPA, routes are just `/showcase`, `/about`, etc. The gateway path prefix doesn't exist from the page's point of view.

## URI Paths (custom routes) are also valid

If a page has a custom URI path defined (`/api/ui/uriPaths`), that path can be used as the linkPath too. Example: a custom URI path `/billing` mapping to the page `accountBilling` means both `/accountBilling` and `/billing` work as linkPath values, and the user-facing URL becomes `/billing`.

## External links

For anything outside the app, use a full absolute URL with scheme:

```
RIGHT:  https://github.com/modlix-india
RIGHT:  https://cxlanding.modlix.com/
WRONG:  //github.com/modlix-india        (protocol-relative; platform won't normalize)
WRONG:  github.com/modlix-india           (no scheme; treated as a relative path)
```

When using `Link.target = "_blank"`, the platform adds `rel="noopener noreferrer"` automatically for external URLs.

## Quick checklist before saving a linkPath

1. Is the destination inside this app? → `/<pageName>`
2. Is it a custom URI path? → `/<customUriPath>`
3. Is it external? → full URL with `https://`

If your linkPath contains the app's own appCode or clientCode, you've made the mistake. Strip the prefix down to just the page name.

## Making non-linkable components clickable

Confirmed 2026-05-18: you cannot make an Image clickable by setting `linkPath` on it because the Image component doesn't have that property in its schema. The platform silently ignores unknown property keys, so the call succeeds but does nothing in the browser.

Two correct patterns:

### Pattern A — wrap in a Grid (preferred for nav)

Add a Grid as the parent, set the Grid's `linkPath`, leave the Image inside untouched:

```
nav (Grid, ROWLAYOUT)
  navLogoLink (Grid, SINGLECOLUMNLAYOUT, linkPath="/homeTwo")    ← new wrapper, clickable
    navLogo (Image, src=…)
  navLinks (Grid, ROWLAYOUT)
    ...
```

Style the wrapper Grid to not add visual weight: no `padding`, `width: auto`, `cursor: pointer` if you want the hand cursor (Grid linkPath does this automatically in most builds, double-check). The Grid renders as a router `<a>` element wrapping the Image.

### Pattern B — use the component's own `onClick` event

Image has an `onClick` property that points to a page-event-function key:

```
properties.onClick.value = "navigateToHome"
```

…where `navigateToHome` is a page event function that calls a Kirun primitive for navigation (typically `UIEngine.NavigateTo` or similar). Heavier than Pattern A because you must also create and wire the event function. Use only when the click needs to do more than just route (e.g. track an analytics event, conditionally route based on auth state).

### How to tell which pattern is right

- Click should just route → Pattern A (Grid wrap)
- Click needs side-effects → Pattern B (onClick event)

Avoid setting `linkPath` on Image, Text, Icon, FormField, or any other component whose schema doesn't include it. Always check via `get_component_schema` if you are not sure.
