# Essence Analyst (`EssenceAnalyst`)

> **Status: implemented; this doc written 2026-09-11** - describes the code as it is.

## Purpose

Tier-3 of the competitor-creative ingest cascade: given the deduped, verified
survivors of one competitor's ad fetch, extract each creative's typed
**`Essence`** in one multimodal pass - what the ad IS (strategy: hook, angle,
awareness stage, offer, proof), what it is ABOUT (subject, OCR text), how it is
BUILT (format, style, layout, colors), and **what it is SELLING** (the
classification block: category / market / offering stage / advertised project /
advertiser role / confidence) that the relevance gate
(`creative_intelligence/taxonomy.py`) judges before anything is written to the
shared CompetitorCreativeLibrary.

It never culls a creative - dedup is deterministic (Tiers 1-2, `dedup.py`),
vision only adds. The GATE culls, downstream in the library, using this
agent's classification.

## Architecture

```
creative_intelligence/library.py (ingest)
   enrich hook (injected - the domain never imports this agent)
        ▲
        │ tools/creatives.py :: _essence_enrich builds the hook
        │
┌───────────────────────────────────────────────────────────────────┐
│  EssenceAnalyst (BaseAgent, tools=[], max_turns=1)                │
│  extract(images, ..., status_tuid, insight_agent_id,              │
│          competitor_name)                                         │
│    · dedupe by content_hash; _drop_undecodable (PIL gate)         │
│    · chunk (≤12 images/call), ≤3 chunks concurrent                │
│    · one LLM call per chunk → EssenceBatch (fenced JSON)          │
│    · unparseable batch → per-creative retry fallback              │
│    · returns {content_hash: Essence}; absent = essence stays None │
└───────────────────────────────────────────────────────────────────┘
```

### File layout

```
app/agents/adzump/agents/creative_essence/
├── agent.py     EssenceAnalyst + get_essence_analyst(), chunking/fallback,
│                _insight_line (card narration), _drop_undecodable,
│                _SilentEventStream
├── context.py   the essence + classification prompt; every enum list is
│                GENERATED from the Essence Literals at import time, so the
│                instruction and the validator can never disagree
└── models.py    EssenceVerdict (= Essence + input-order idx) + EssenceBatch -
                 the wire shape INHERITS the domain model, one schema source
                 of truth (enums live on creative_intelligence/models.py)
```

The input unit (`CreativeImage`) and the hook Protocol (`EnrichCreatives`) are
the domain's seam - `creative_intelligence/enrich.py`.

## Provider configuration

| Constant | Value | Why |
|---|---|---|
| `ESSENCE_MODEL_OVERRIDE` | `deepseek:deepseek-v4-flash-vision-exp` | 2026-09-10 bench (`scripts/bench_essence.py`, report in `logs/bench_essence_report.md`): grounded hooks where gpt-4o-mini fabricated text, reads on-image prices verbatim, ~20x cheaper vision input, streams reasoning. Trade-off: ~4x slower - fine for a background enrich |
| `ESSENCE_MAX_TOKENS` | `4000` | ~150-200 tokens per verdict x 12-image chunk, well under truncation |
| `MAX_IMAGES_PER_CALL` | `12` | one call in the common post-dedup case |
| `MAX_CONCURRENT_CALLS` | `3` | a 60-creative competitor must not fire 5 vision calls atop the other competitors' pipelines |

## The extraction contract

- Input: `list[CreativeImage]` - each carries the stored `Creative` + the
  rehosted bytes (the SAME bytes the rehost hashed, so `content_hash` agrees).
  The user message carries each ad's copy + landing URL, and the competitor's
  name (used ONLY for `advertiser_role` - a different developer's project =
  broker; never as a category signal).
- Output: `{content_hash: Essence}`. A verdict that never parses is simply
  absent - its stored essence stays None, the library's gate then rejects that
  creative fail-closed, and the next real ingest re-attempts. Never raises;
  total failure returns `{}`.
- Lenient enums: an off-list model value (including an invented `category`)
  coerces to the field default instead of failing the batch (live 2026-09-08:
  6 invalid values failed a whole 12-image batch). `category` defaults to
  `unknown`, which the gate rejects - a hallucinated bucket can neither crash
  the batch nor sneak an ad in.
- `taxonomy_version` is NOT stamped here - the library stamps it at assignment
  so carry-forward and re-classification key off one place.

## Hardening history (why the odd bits exist)

- `_drop_undecodable` (PIL verify): one corrupt mention-tier image 400'd the
  whole OpenAI vision call and wedged the batch 12+ minutes (live 2026-09-10).
- Per-creative fallback: an unparseable BATCH retries each image alone, so one
  bad verdict costs one creative, not twelve.
- The library wraps the whole hook in `_ENRICH_TIMEOUT_SECONDS` - a hung
  vision call degrades to essence-less creatives, never a stuck spinner.

## Card narration (observability)

The inner stream is silent by design (`_SilentEventStream`); live progress
rides the COMPETITOR's card row instead:

- `status_tuid` → `emit_tool_update` status lines ("Reading 12 ad creatives…",
  "Analyzed 8/12 creatives…").
- `insight_agent_id` → one `_insight_line` per verdict streamed as the row's
  thinking quote: `offer-led · "pay 10% now, rest on possession" · static image`.
- Nested mode (insight_agent_id set) never emits its own `agent_finished` -
  the launcher (`tools/creatives.py::_CompetitorSpans`) owns the span close
  and its rollup summary; an all-failed run posts an honest status line
  ("couldn't read these creatives - will retry on the next fetch").

## Callers

Exactly one production path: `tools/creatives.py::_essence_enrich` builds the
hook and `creative_intelligence/library.py::_enrich_essence` awaits it during
a real ingest - never on a cache hit, and only for survivors that still lack
essence (or whose essence predates the current `TAXONOMY_VERSION`).

## Testing

| File | Covers |
|---|---|
| `tests/agents/adzump/agents/creative_essence/test_essence.py` | parse/collect/shrink seams, enum coercion (incl. classification fields), message building (copy + landing URL + competitor name), chunking/fallback/concurrency, insight lines, undecodable drop |
| `tests/agents/adzump/creative_intelligence/test_library.py` | the ingest wiring around the hook: carry-forward, timeout, the relevance gate over this agent's output |

## Design decisions

- **Wire shape inherits the domain model.** `EssenceVerdict(Essence)` + enums
  generated into the prompt = the vision contract and the stored schema cannot
  drift.
- **Injected hook, model-free domain.** `creative_intelligence/` defines the
  `EnrichCreatives` Protocol and never imports this agent; the tool wires it.
- **Content-addressed essence.** Keyed by `content_hash`, so a refetch that
  re-lists the same images costs zero vision calls (the library carries prior
  essences forward by hash, version-checked).
- **Classify the AD, never the advertiser.** The prompt forbids using the
  competitor's name/domain as a category signal - the gate exists precisely
  because a same-market competitor can run an ad for something else.
