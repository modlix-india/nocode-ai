# Implementation Notes: Craft redesign - Phase A (backend recompose)

Spec: `MOD_AI/plans/craft-competitor-redesign.html` + the hi-fi journey mockup
`MOD_AI/plans/craft-redesign-mockup.html`.

**This slice (Phase A, backend-only - no frontend change):**
- Competitors: stacked key→value text cards → a comparison **table** (Rival · Format ·
  Pricing · Gap) + a per-rival **collapsible** holding Location / USPs / Why / Website.
- Creatives: one-column image strip → **metric** tiles (Total · Active · Paused) + a
  **2-up image grid** (rows of two `image` blocks).

**Files changed (working tree, branch `feat/adlibrary-creative-library`, uncommitted):**
- `app/agents/adzump/tools/craft.py` - `render_competitors()`
- `app/agents/adzump/tools/creatives.py` - `_render_creatives()`

## Decisions Not in the Spec
- **Competitor table 4th column = Gap, not an ad count**: ad counts aren't known during
  competitor *analysis* (creatives are fetched later by a separate tool), so the
  integrated "Ads" column from the mockup can't be populated here. Used the strategic
  Gap (`weakness`) instead - matches the plan's reconciliation note.
- **Collapsible holds Location/USPs/Why/Website; Gap stays only in the table** - avoids
  showing Gap twice. Location was demoted from the old card into the collapsible (most
  real-estate rivals share the same locale, so it's low-signal in the at-a-glance table).
- **Three creative metrics (Total · Active · Paused), Paused derived** as `total-active`.
  Did not add the mockup's "% active" tile - it's trivially re-derivable and I kept the
  backend to values that exist on the record.
- **Caption moved onto the `image` block** (`ImageBlock.caption`) instead of a separate
  trailing `text` block. A separate text block would land as its own grid cell and break
  the 2-up row; an image caption renders directly under its own thumbnail.
- **2 images per row, not 3** - fits the ~580px panel and matches the mockup's 2-up grid.

## Changes from the Spec
- **Targets moved post-merge.** The plan's `competitor.py:171 _render_competitors` and
  `creatives.py` line refs were pre-merge. After merging master, competitor rendering
  lives in `tools/craft.py:render_competitors` (the panel is assembled by
  `emit_craft_panel`); creatives still render in `tools/creatives.py:_render_creatives`.
  Implemented against the current code.
- **Dropped the planned `fit:"cover"` hint on creative images** - the base
  `_craftImage img` CSS already defaults to `object-fit:cover` with a 16:10 aspect ratio,
  and the `_cover` class is thumbnail-scoped, so the hint would add an unstyled class for
  no effect.

## Tradeoffs
- **`row`-of-images grid vs a true gallery.** Chose composing existing blocks (Phase A,
  zero frontend change, ships independently). Gave up: hover states, platform/Active
  badges, client-side filtering, per-rival tabs, and "Use as inspiration" - all of which
  need the `creative_gallery` block (Phase B). Also, a trailing odd creative stretches
  full-width rather than sitting in a half-cell.
- **No new constants/caps touched.** Kept the existing 20-competitor and
  `_RENDER_PER_COMPETITOR = 6` caps rather than retuning them in this slice.

## Fix: disappearing creative grid (Option A) - 2026-06-29

**Bug (found via live Playwright test on creative-lb):** competitor creatives fetched
fine (84 ads, rehosted) but the grid vanished from the panel. Cause: `_render_creatives`
**appended** the grid (`append=True`), but the full-panel rebuild `emit_craft_panel`
(`append=False`, called from product/geo/competitor/scrape on every new fact) rebuilds
product+targeting+competitors and **did not include creatives** - so the next rebuild
wiped the appended grid. Pre-existing fragility (the old image-strip had it too); it
only surfaced when the flow continued past the fetch and triggered a rebuild.

**Fix - render creatives as part of the rebuild (single source of truth):**
- `tools/craft.py`: added `render_competitor_creatives(blocks, name, creatives, total,
  active)` - the shared block builder (metric tiles Total/Active/Paused + 2-up image
  grid, cap `_RENDER_PER_COMPETITOR=6`). `emit_craft_panel` now loops competitors after
  `render_competitors` and draws each one's grid from `comp["creatives"]` /
  `comp["creativeStats"]` (present once `fetch_competitor_creatives` has run).
- `tools/creatives.py`: `_render_creatives` is now a thin wrapper that calls the same
  `render_competitor_creatives` then emits `append=True`. So the incremental append and
  the rebuild produce identical blocks; a rebuild redraws the creatives instead of
  dropping them.

**Verified:** py_compile OK; structural test confirms `emit_craft_panel` rebuild now
contains the "Ad Creatives" heading + metric tiles; **live on creative-lb (Playwright)**
- grid renders (Brigade Orchards 24/0/24, Embassy Springs 60/8/52 with 2-up image grids)
and **survives advancing the flow** (picked duration → grid stayed).

**Heads up on this fix:** every rebuild now redraws all competitors' creatives (capped 6
each) - fine (lazy-loaded image URLs). Order: each rival's grid sits under the
competitors table (today's layout); the unified tabbed gallery is still Phase B.

## Heads Up
- **Phase B is not done**: the interactive `creative_gallery` block (tabs +
  platform/Active filters + "Use as inspiration") - plan narrow-path steps 3–6 - needs a
  new `CraftRenderer.tsx` renderer + a backend block. Selection is inert until the
  creative agent exists.
- **Verification**: block schemas were checked against the live renderer
  (`table`/`metric`/`row`/`collapsible`/`image` all registered and styled in
  `CraftRenderer.tsx` + `PromptStyle.tsx`), and the Python compiles. A live re-run of
  `analyze_competitors` + `fetch_competitor_creatives` is still worth eyeballing - the
  running `:5001` (`--reload`) picks up these edits automatically.
- **Missing fields** render as `-` in the table (some rivals have no Pricing/Format).
- Lands mixed with pre-existing uncommitted dev edits on the branch; **not committed**,
  per the earlier instruction.
