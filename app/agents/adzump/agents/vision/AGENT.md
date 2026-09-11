# Vision Analyst (`VisionAnalyst`)

> **Status: implemented; this doc written 2026-09-11** - describes the code as it is.

## Purpose

Single-shot vision judgment over images. Two modes, one engine:

- **select-subset** (the scrape path): given the site's candidate images +
  a full-page screenshot, pick the brand logo(s) and the ad-creative-worthy
  images, with roles (hero / amenity / floor_plan) and a launch-readiness
  verdict.
- **review-each** (the upload path): one verdict per user-uploaded image
  (is it a logo? a creative? junk?).

It is the sole authority on logo picks - there is no deterministic logo-filling
fallback (deleted in v9, 2026-05-22). When it declines, `logos=[]` and the user
uploads via the AdPilot UI.

## Architecture

```
product_assets.py (scrape)                tools/asset_manage.py (upload)
   get_selector().pick(...)                  get_reviewer().review(...)
        │                                          │
        ▼                                          ▼
┌───────────────────────────────────────────────────────────────────┐
│  VisionAnalyst (BaseAgent, tools=[], max_turns=1, gpt-4o-mini)    │
│  TWO singleton instances - the system prompt can't be swapped per │
│  run, so each mode is its own configured instance:                │
│    · vision_select  (build_select_context)  → AssetSelection      │
│    · vision_review  (build_review_context)  → ReviewResult        │
│  fenced-JSON final message → pydantic parse → typed result        │
└───────────────────────────────────────────────────────────────────┘
```

### File layout

```
app/agents/adzump/agents/vision/
├── agent.py     VisionAnalyst + get_selector()/get_reviewer(),
│                _resolve_picks (indices→ProductAssets), _SilentEventStream,
│                _build_user_message_and_images / _build_review_message
├── context.py   build_select_context() + build_review_context()
└── models.py    AssetSelection (LogoChoice/CreativeChoice) + ReviewResult
                 (ImageVerdict) - the LLM's wire shapes
```

The RESOLVED public types (`LogoPick`, `CreativeRole`, `ProductAssets`,
`CreativeCompleteness`) still live in `agents/product/models.py` (D4: a future
cross-agent extraction).

## Provider configuration

| Constant | Value | Why |
|---|---|---|
| `VISION_MODEL_OVERRIDE` | `openai:gpt-4o-mini` | cost parity with the direct call it replaced (~20x cheaper than Sonnet for the same task); a DeepSeek bench is pending (the essence analyst already moved) |
| `VISION_MAX_TOKENS` | `600` | output is one small JSON object |
| `VISION_MAX_TURNS` | `1` | single shot, no tools |

## select-subset: how a pick works

1. Caller (`product_assets.select_product_assets`) prefilters candidates,
   fetches thumbnails, and passes `candidates` + `fetched` bytes + business
   summary + metadata JSON + the full-page screenshot.
2. `_build_user_message_and_images`: screenshot rides as image block **#0**
   (spatial context: header strip = logos, partner footer = NOT logos, hero
   band = hero creative), candidates follow as blocks #1..N in index order;
   SVGs are text-only entries (no thumbnail).
3. The model answers with index-based picks (`AssetSelection`).
4. `_resolve_picks` maps indices → URLs, dedupes, drops out-of-bounds, applies
   the one deterministic guard (`_filename_suggests_logo` drops a logo-named
   URL that slipped into the creative bucket, e.g. `clublogo.png`), and
   **computes** `CreativeCompleteness` in code (model labels, code applies the
   launch-readiness policy: complete = hero AND >=1 amenity; floor_plan tracked
   but never required).

Failure contract: any run/parse failure returns an empty
`ProductAssets()` / `ReviewResult()` - downstream treats empty as a decline
(upload path), never a crash.

## Event streaming

`_SilentEventStream` drops everything except `agent_started/finished/usage` +
`data` - the analyst surfaces no text/thinking; only its AgentCard span shows.
`agent_finished.summary` follows the right-meta contract: only span outcomes
not visible elsewhere (`"logos=N creatives=N"`, or the exception name on
error; see the docstring in `summary/agent.py::_emit_finished` for the rule).

## Callers

| Caller | Mode | Entry |
|---|---|---|
| `agents/product/product_assets.py` | select-subset | `get_selector().pick(...)` |
| `tools/asset_manage.py` | review-each | `get_reviewer().review(...)` |

## Testing

| File | Covers |
|---|---|
| `tests/agents/adzump/agents/vision/test_resolve_picks.py` | index→URL resolution, dedupe, OOB, filename guard, completeness derivation |
| `tests/agents/adzump/agents/vision/test_select_seams.py` | message building (screenshot block #0, SVG text-only), parse failures → empty |
| `tests/agents/adzump/agents/vision/test_vision_review.py` | review-each verdict parsing + failure contract |

## Design decisions

- **An agent, not a bare completion call** - Adzump's convention: every LLM
  work unit is a BaseAgent subclass (token tracking, audit sub-session,
  observability card). This replaced a direct `client.beta.chat.completions.parse`
  call.
- **Model perceives, code derives.** Roles come from the model; the
  launch-readiness verdict and missing-categories list are computed in
  `_resolve_picks` (Kiran's Q3 pick) - policy changes never need a prompt edit.
- **Two instances over prompt-swapping** - BaseAgent builds the system prompt
  at construction; a per-run swap would be a hidden mode flag.
