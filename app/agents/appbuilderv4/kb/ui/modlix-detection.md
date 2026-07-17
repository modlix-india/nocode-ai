---
name: Modlix Site Detection & Hybrid Clone
description: Clone pipeline detects Modlix sites via getStore() for 100% fidelity; hybrid clone combines screenshot layout + DOM images
type: project
originSessionId: 892cf529-5a4c-4bd1-88ca-3b384ca0b697
---
## Modlix Site Detection + Hybrid Clone Improvements (2026-04-14)

### Modlix Detection (Phase 0)
When cloning a URL, the pipeline first checks if the target is a Modlix site by running `getStore().pageDefinition` in Playwright's console. If detected, extracts the full componentDefinition JSON directly — no AI, no screenshot analysis, 100% fidelity.

### Key files
- `app/agents/appbuilder/tools/_shared.py` — `detect_modlix_site()` + `ModlixPageData` dataclass
- `app/agents/appbuilder/tools/clone_tool.py` — `_modlix_fast_clone()` helper, Phase 0 check
- `app/agents/appbuilder/tools/hybrid_clone.py` — Step 0 detection + full hybrid pipeline

### Hybrid clone fixes (this session)
1. **CDN URL unwrapping**: speedsize.com wraps real URLs in `cdn.speedsize.com/UUID/https://real-url`. DOM extraction now unwraps in-browser via `unwrapCdn()` JS function, stores both unwrapped and CDN src for download fallback.
2. **Image-to-section matching**: Changed from strict y-range `[y_start, y_end]` to padded `[y_start-200, y_end+200]` with deduplication to catch boundary images.
3. **URL truncation removed**: Was `[:120]` in prompt which cut long CDN URLs. Now sends full URLs.
4. **Image download resilience**: Shared httpx client with Referer header, tries unwrapped URL first then CDN fallback. 0 failures on ultrahuman (was many 404s).
5. **Image sizing prompt**: Instructs gpt-4o to make full-width images use `width:100%, objectFit:cover`, not fixed small dimensions.
6. **Text color correction**: Now reads LLM-set `backgroundColor` from section root style, not just pixel-sampled avg_bg_color.

### Test results (final, 2026-04-14)
- **cityville.in**: Modlix detected, 476 components, **100% fidelity**, 10.8s
- **petefreitag.com**: DOM clone, 132 components, **91.9% avg** (94.9% desktop), 43.6s
- **ultrahuman.com/in/**: Hybrid clone, 89 components, **22 images re-uploaded**, ~62% pixel (visually good — pixel score misleading on 12000px tall pages), 154.5s

**Why:** Modlix sites can be perfectly cloned without any AI. For non-Modlix sites, hybrid clone extracts real images from DOM instead of using placeholders.
**How to apply:** Both `clone_website` and `hybrid_clone` run detection as Phase 0/Step 0. HYBRID_CLONE registered in registry.py as deferred tool.
