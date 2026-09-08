# UI — fontPacks shape (CRITICAL)

`app.properties.fontPacks` is consumed by the platform's React runtime
at `src/App/App.tsx:processFontPacks`. Get the shape wrong and the page
crashes with `Cannot read properties of undefined (reading 'trim')`
during render.

## The correct shape

```json
{
  "fontPacks": {
    "<uuid>": {
      "name": "Inter",
      "code": "<link rel='preconnect' href='https://fonts.googleapis.com'><link rel='preconnect' href='https://fonts.gstatic.com' crossorigin><link href='https://fonts.googleapis.com/css2?family=Inter:wght@100..900&display=swap' rel='stylesheet'>"
    },
    "<another-uuid>": {
      "name": "Asap",
      "code": "<link href='...' rel='stylesheet'>"
    }
  }
}
```

Rules:
- Top-level keys: ANY UUID (use `modlix.uuid()`). One key per font family.
- Each entry MUST have:
  - `name` — string, the font family name as it will be used in
    `styleProperties.fontFamily`. The runtime trims this with `.trim()`.
  - `code` — string of literal HTML to inject into the page `<head>`.
    Typically Google Fonts `<link>` tags. The runtime trims this too.

Both fields must be PRESENT and STRING-TYPED. Omitting either causes a
crash.

## What does NOT work

```json
// CRASHES: per-family list of src URLs is not the platform's shape.
"fontPacks": {
  "Inter": [{"src": "...", "weight": "400", "style": "normal"}]
}
```

Looks reasonable as a webfont API but the runtime expects `value.code`,
not a list. The error surfaces as `undefined.trim()` on the FIRST page
load attempt.

## Google Fonts (the common case)

For Google-hosted fonts, just paste the Google Fonts embed snippet into
`code`:

```json
{
  "<uuid>": {
    "name": "Inter",
    "code": "<link rel='preconnect' href='https://fonts.googleapis.com'><link rel='preconnect' href='https://fonts.gstatic.com' crossorigin><link href='https://fonts.googleapis.com/css2?family=Inter:wght@100..900&display=swap' rel='stylesheet'>"
  }
}
```

## Self-hosted .woff2 (e.g. from extract_site_fonts)

When you've downloaded a source site's `.woff2` files and uploaded them
to Modlix files (via `extract_site_fonts` or similar), reference them
with an inline `<style>` containing a custom `@font-face`:

```json
{
  "<uuid>": {
    "name": "InterVariable",
    "code": "<style>@font-face{font-family:'InterVariable';src:url('http://localhost:8080/api/files/static/file/SYSTEM/<app>/global/fonts/InterVariable_xxx.woff2') format('woff2-variations');font-weight:100 900;font-style:normal;font-display:swap;}</style>"
  }
}
```

Multiple weights/styles for one family go in a SINGLE `<style>` block
with multiple `@font-face` declarations.

## Applying the font in components

Once `fontPacks` is registered AND the page reloads, components reference
the family by `name`:

```python
'styleProperties': {
    '<uuid>': {
        'resolutions': {
            'ALL': {'fontFamily': {'value': "'InterVariable', system-ui"}}
        }
    }
}
```

The platform serves the `code` HTML in the page head; the browser loads
the font; the styleProperties match by family name.

## How to PUT fontPacks correctly

Use the same fetch-merge-PUT pattern as page replace — the platform's
UI app PUT replaces `properties` wholesale, so missing a field zeroes
it out:

```python
docs = modlix.get('/api/ui/applications', params={'appCode': '<app>'})['content']
mongo_id = docs[0]['id']
doc = modlix.get(f'/api/ui/applications/{mongo_id}')   # FULL doc by id

props = doc.get('properties') or {}
props['fontPacks'] = {<your uuid-keyed map>}
doc['properties'] = props
doc['message'] = 'Register fontPacks'

modlix.put(f'/api/ui/applications/{mongo_id}', body=doc)
```

## Reference apps with correct fontPacks

- `appbuilder` (v101+): 2 Google Fonts (ASAP + Inter)
- `cxapp`, `leadzump`, `landingpages`: same pattern, varying families

Inspect any of them to see live examples:
`modlix.get(f'/api/ui/applications/{mongo_id_of_<refapp>}')`
