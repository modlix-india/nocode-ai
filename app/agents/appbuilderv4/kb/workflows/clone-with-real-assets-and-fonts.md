# Workflow: clone-with-real-assets-and-fonts

**Goal:** Produce a high-fidelity clone of an external page using the source's
ACTUAL images, ACTUAL videos, ACTUAL web fonts — not placeholders, not system
fallbacks.

**Touches services:** ui, shared (files)

## Why this exists

Without this discipline, agent clones land at ~50% similarity max:
- Image components bind to invented placeholders ("apple.png from Wikipedia").
- Fonts never load, so the browser falls back to system-ui and typography drifts.
- `compare_to_source` permanently shows medium/high diffs for these.

## Steps

### 1. ONE call to `extract_site_assets` harvests everything

```python
import modlix
# from the tool result: r.data is the manifest below.
# extract_site_assets does it all in one Playwright pass:
#   - viewports.<w>.{fullpage_handle, sections[], hovers[], animations[], font_stack}
#   - assets[]            (images + svgs + bg images)
#   - videos[]            (mp4 / webm with optional poster)
#   - fontPacks_suggested (UUID-keyed {name, code} ready for app.properties.fontPacks)
```

There is no longer a separate `screenshot_external_url` or `extract_site_fonts`
tool — they were folded in. Calling `extract_site_assets` ONCE per session is
all you need.

### 2. Register the fontPacks at app level (USE CORRECT SHAPE)

The platform's React runtime (`src/App/App.tsx:processFontPacks`)
expects `app.properties.fontPacks` to be a UUID-keyed map of
`{name, code}` where `code` is literal HTML injected into the page
head. Get the shape wrong and the page crashes with
`undefined.trim()`. See `ui/font-packs.md` for the full reference.

The `fontPacks_suggested` field in the result is ALREADY in the
correct UUID-keyed shape:

```python
# extract_site_assets result data['fontPacks_suggested'] looks like:
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
props['fontPacks'] = recon_result['data']['fontPacks_suggested']
doc['properties'] = props
doc['message'] = 'Register source fonts'

modlix.put(f'/api/ui/applications/{mongo_id}', body=doc)
```

After this PUT, the browser sees `<style>@font-face{...}</style>` in the
page head on next load, and components referencing the family by name in
`styleProperties.fontFamily` actually render with the right font.

### 3. Build a manifest map by ROLE + DIMENSIONS

The `extract_site_assets` result `data['assets']` has entries shaped like:
```json
{"src": "https://linear.app/.../mockup.png",
 "kind": "img",
 "modlix_url": "http://localhost:8080/api/files/static/file/SYSTEM/vclone/global/clone/img_abc123.png",
 "role": "hero" | "header" | "footer" | "content" | "bg" | "body-bg",
 "width": 1416, "height": 768, "alt": "Linear app interface"}
```

Pick assets like this:
- **Hero / above-the-fold imagery** → entries with `role='hero'`, largest width.
  If `compare_to_source` flags a missing product-mockup, the answer is
  ALMOST ALWAYS the largest landscape asset (aspect ratio ≥ 1.5:1) in this
  list. Bind that as ONE `Image` component. Do NOT decompose the mockup
  into stacked text components.
- **Navbar / logo** → entries with `role='header'` and small dimensions
- **Customer logo strip** → multiple `role='content'` entries with similar small dimensions
- **Feature mockups** → `role='content'` with medium-to-large dimensions
- **Footer / social icons** → `role='footer'`

### 4. For EACH Image component, bind a real modlix_url

```python
# WRONG — invents a URL
{'type': 'Image', 'properties': {'src': {'value': 'https://example.com/placeholder.png'}}}

# RIGHT — uses the manifest entry's modlix_url
hero_asset = next(a for a in recon_result['data']['assets']
                  if a['role'] == 'hero' and a['width'] > 1000)
{'type': 'Image', 'properties': {'src': {'value': hero_asset['modlix_url']}}}
```

### 5. Videos — same pattern, Video component

```python
# data['videos'] entries look like:
# {"src": "...", "modlix_url": "...", "poster_url": "...",
#  "width": 1280, "height": 720, "autoplay": true, "loop": true, "muted": true}

for v in recon_result['data']['videos']:
    {'type': 'Video',
     'properties': {
         'src': {'value': v['modlix_url']},
         'poster': {'value': v.get('poster_url') or ''},
         'autoplay': {'value': v['autoplay']},
         'loop': {'value': v['loop']},
         'muted': {'value': v['muted']},
     }}
```

### 6. Apply the fonts in styleProperties

After registering fontPacks, reference fonts by family name (the `font_stack`
field in the per-viewport manifest tells you what the source actually uses):
```python
# data['viewports']['1440']['font_stack'] looks like:
# {"h1": "'Inter Display', 'Inter', sans-serif", "body": "'Inter', sans-serif", ...}

font_stack = recon_result['data']['viewports']['1440']['font_stack']
'styleProperties': {
    'hero-heading-rule': {
        'resolutions': {
            'ALL': {
                'fontFamily': {'value': font_stack.get('h1', 'sans-serif')},
                'fontWeight': {'value': '510'},  # match the source weight
                'fontSize': {'value': '62px'},
            }
        }
    }
}
```

### 7. Hovers + animations — separate workflow

Hover-revealed UI (`viewports.<w>.hovers[]`) and animations
(`viewports.<w>.animations[]`) are NOT optional. See the
`clone-render-hovers-and-animations` workflow for the full recipe (Popover
vs Grid+visibility-binding, keyframes vs transitions vs scroll-triggered).

### 8. Compare per section and iterate

`compare_to_source(page_name='home', source_handle='linear-app__root:section_hero_w1440')`
diffs ONE section against your build's screenshot. Per-section is faster
than full-page — the diff scopes to the part you're working on.

After steps 1-7 land, compare should now see:
- Real imagery in the right places (no "broken hero image" / "placeholder URL" diffs)
- Correct fonts loaded (typography diffs drop from `high` to `low`)
- Hover menus appear on hover (no "missing dropdown" diff)
- Animations play (no "no motion" diff)

Remaining diffs are about layout/spacing/exact color — fixable with focused
patches, not whole-page rebuilds.

## Anti-patterns

- ❌ Composing the page with placeholder URLs, planning to "go back and fix later" — the agent never does.
- ❌ Calling `extract_site_assets` but NOT walking the manifest before composing — every Image component should be bound from the manifest at AUTHOR time, not as a follow-up.
- ❌ Skipping font registration because "system fonts look close enough" — they don't. Source typography is part of source identity.
- ❌ Using `generate_image` (Gemini Nano Banana) for content imagery on a clone — that's hallucinating images that should be the real ones.
- ❌ Re-running `extract_site_assets` multiple times in one session. ONCE per session. The manifest doesn't go stale.
- ❌ OCR-decomposing a product-mockup PNG into 30 stacked text components.
  When compare flags "missing product-mockup", bind the largest landscape
  asset as an Image. That's the entire fix.

## Related workflows

- `clone-render-hovers-and-animations`: render the hover + animation
  catalog as live Modlix UI. RUN THIS AFTER step 6 above.
- `replace-page-atomic`: how to PUT the composed page back.
- `create-app-full`: required if the target app doesn't exist yet.
