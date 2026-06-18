# Workflow: clone-with-real-assets-and-fonts

**Goal:** Produce a high-fidelity clone of an external page using the source's
ACTUAL images and ACTUAL web fonts — not placeholders, not system fallbacks.

**Touches services:** ui, shared (files)

## Why this exists

Without this discipline, agent clones land at ~50% similarity max:
- Image components bind to invented placeholders ("apple.png from Wikipedia").
- Fonts never load, so the browser falls back to system-ui and typography drifts.
- `compare_to_source` permanently shows medium/high diffs for these.

## Steps

### 1. Harvest images AND fonts BEFORE composing the page

```python
import modlix

assets = ...  # via extract_site_assets tool: harvests imgs/svgs/bg-images
fonts  = ...  # via extract_site_fonts  tool: harvests @font-face files
```

Both upload into `<app>/global/clone/` (images) and `<app>/global/fonts/` (fonts).

### 2. Register the fontPacks at app level (USE CORRECT SHAPE)

The platform's React runtime (`src/App/App.tsx:processFontPacks`)
expects `app.properties.fontPacks` to be a UUID-keyed map of
`{name, code}` where `code` is literal HTML injected into the page
head. Get the shape wrong and the page crashes with
`undefined.trim()`. See `ui/font-packs.md` for the full reference.

After `extract_site_fonts`, the `fontPacks_suggested` field in the
result is ALREADY in the correct UUID-keyed shape:

```python
# extract_site_fonts result data['fontPacks_suggested'] looks like:
{
  "<uuid1>": {
    "name": "InterVariable",
    "code": "<style>@font-face{font-family:'InterVariable';src:url('...woff2') format('woff2-variations');font-weight:100 900;font-style:normal;font-display:swap;}</style>"
  },
  "<uuid2>": { ... }
}
```

PUT it into the app (fetch-merge-PUT, not blind overwrite):

```python
import modlix
docs = modlix.get('/api/ui/applications', params={'appCode': '<app>'})['content']
mongo_id = docs[0]['id']
doc = modlix.get(f'/api/ui/applications/{mongo_id}')  # full detail by id

props = doc.get('properties') or {}
props['fontPacks'] = font_extract_result['data']['fontPacks_suggested']
doc['properties'] = props
doc['message'] = 'Register source fonts'

modlix.put(f'/api/ui/applications/{mongo_id}', body=doc)
```

After this PUT, the browser sees `<style>@font-face{...}</style>` in the
page head on next load, and components referencing the family by name in
`styleProperties.fontFamily` actually render with the right font.

### 3. Build a manifest map by ROLE + DIMENSIONS

The `extract_site_assets` result has entries shaped like:
```json
{"src": "https://linear.app/.../mockup.png",
 "modlix_url": "http://localhost:8080/api/files/static/file/SYSTEM/vclone/global/clone/img_abc123.png",
 "role": "hero" | "header" | "footer" | "content" | "bg" | "body-bg",
 "width": 1416, "height": 768, "alt": "Linear app interface"}
```

Pick assets like this:
- **Hero / above-the-fold imagery** → entries with `role='hero'`, largest width
- **Navbar / logo** → entries with `role='header'` and small dimensions
- **Customer logo strip** → multiple `role='content'` entries with similar small dimensions
- **Feature mockups** → `role='content'` with medium-to-large dimensions
- **Footer / social icons** → `role='footer'`

### 4. For EACH Image component, bind a real modlix_url

```python
# WRONG — invents a URL
{'type': 'Image', 'properties': {'src': {'value': 'https://example.com/placeholder.png'}}}

# RIGHT — uses the manifest entry's modlix_url
hero_asset = next(a for a in manifest['originals']
                  if a['role'] == 'hero' and a['width'] > 1000)
{'type': 'Image', 'properties': {'src': {'value': hero_asset['modlix_url']}}}
```

### 5. Apply the fonts in styleProperties

After registering fontPacks, reference fonts by family name:
```python
'styleProperties': {
    'hero-heading-rule': {
        'resolutions': {
            'ALL': {
                'fontFamily': {'value': "'Inter Display', 'Inter', sans-serif"},
                'fontWeight': {'value': '510'},  # match the source weight
                'fontSize': {'value': '62px'},
            }
        }
    }
}
```

### 6. Compare and iterate

`compare_to_source` will now see:
- Real imagery in the right places (no more "broken hero image" or "placeholder URL" diffs)
- Correct fonts loaded (typography diffs drop from `high` to `low`)

Remaining diffs are about layout/spacing/animations — fixable with focused
patches, not whole-page rebuilds.

## Anti-patterns

- ❌ Composing the page with placeholder URLs, planning to "go back and fix later" — the agent never does.
- ❌ Calling `extract_site_assets` but NOT walking the manifest before composing — every Image component should be bound from the manifest at AUTHOR time, not as a follow-up.
- ❌ Skipping `extract_site_fonts` because "system fonts look close enough" — they don't. Linear's typography is its identity.
- ❌ Using `generate_image` (Gemini Nano Banana) for content imagery on a clone — that's hallucinating images that should be the real ones.

## Related workflows

- `extract_site_assets` (tool, not workflow): harvests images
- `extract_site_fonts` (tool, not workflow): harvests fonts
- `replace-page-atomic`: how to PUT the composed page back
- `create-app-full`: required if the target app doesn't exist yet
