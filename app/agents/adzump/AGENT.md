# Adzump Orchestrator (`AdzumpAgent`)

> Describes the code as it is. Sub-agents have their own AGENT.md files (see
> the routing table).

## Purpose

The conversational agent behind **AdPilot** chat: builds a complete ad campaign
(product profile → location → platform → geo targeting → competitors +
creatives → duration/budget → accounts → launch) through multi-turn
conversation. It is a ROUTER and state-keeper - research, geo work, and vision
judgments are delegated to sub-agents; the orchestrator's own LLM decides only
"what happens this turn".

## Core design: steer through the dynamic context, not the loop

One `BaseAgent` tool loop, unmodified. Every ounce of steering rides the
per-turn dynamic context (`agent.py::build_dynamic_context` /
`build_turn_reminder`), rendered by `prompt_sections.py`:

1. `## State` - what's collected, with provenance ("just set" / "set N turns ago")
2. `## User just said` - the last user message verbatim
3. `## What's still missing` - the ordered list from the journey engine
4. `## How to respond` - the priority rule for the LLM

The static system prompt (`context.py`) carries persona + non-negotiable rules
only. The model is re-grounded EVERY agentic turn, so a long session can't
drift off the funnel.

```
┌──────────────────────────────────────────────────────────────────────┐
│  POST /adzump/chat (router.py, SSE)                                  │
└──────────────────────────────┬───────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│  AdzumpAgent (BaseAgent, provider=ADZUMP_PROVIDER, default deepseek) │
│                                                                      │
│  turn start (code, before the LLM):                                  │
│    capture rails - tagged chip answers, prose declines,              │
│    elicitation resume, stale-rail expiry (STALE_RAIL_TURNS=4)        │
│                                                                      │
│  per-turn reminder (code):                                           │
│    AdzumpContext.from_session → missing_list(NEW_CAMPAIGN, actx)   │
│    → the ONE prescribed next action                                  │
│                                                                      │
│  the LLM picks tools (registry.ALL_TOOLS):                           │
│    analyze_product ──────────► ProductAgent (agents/product/)        │
│    analyze_competitors ──────► ProductAgent (full discovery)         │
│    manage_targeting_locations► LocationAgent (agents/location/)      │
│    fetch_competitor_creatives► creative_intelligence + EssenceAnalyst│
│    manage_assets ────────────► VisionAnalyst review (uploads)        │
│    set_campaign_spec / present_options / confirm_location /          │
│    show_campaign_summary / launch_campaign / ...                     │
│                                                                      │
│  loop complete (code): autosave to AISuggestedData, re-map targets   │
│  on a platform switch, restore stored targeting panel, suggestions   │
└──────────────────────────────────────────────────────────────────────┘
```

## File structure

```
app/agents/adzump/
│   the loop
├── agent.py                  AdzumpAgent - the loop, capture rails, per-turn context
├── context.py                static system prompt (persona + non-negotiables, cached)
├── prompt_sections.py        per-turn section renderers (State / User said / Missing / How to respond)
├── workflow.py               journey engine - AdzumpContext, Step, NEW_CAMPAIGN, missing_list
├── observability.py          the per-turn `turn_decision` record
├── router.py                 POST /chat (SSE) + the folded-in location search route
├── products_router.py        GET products / competitors / creatives, DELETE product (UI reads)
│
│   domain rules everyone reads
├── platform.py               single source of truth for the campaign ad-platform
├── answer_parse.py           canonical duration/budget reading for write-boundary validation
├── config.py                 agent-level credential config
│
│   leading underscore = shared helpers, no domain decisions of their own
├── _shared.py                tool utilities (headers, JSON extraction, host parsing)
├── _uploads.py               image upload + rehost pipeline
│
├── stores/                   data access - SQL only, typed models in and out (migration V19)
│   ├── products.py           adzump_products
│   ├── flows.py              adzump_flows - per-flow draft state, resume source
│   └── competitors.py        adzump_competitors + creatives + creative_assets
│
├── models/                   the typed state the orchestrator owns
│   ├── product.py            product_data
│   ├── place.py              the ONE campaign location
│   ├── campaign_spec.py
│   ├── competitor_profile.py session competitor entries (+ attached creatives)
│   └── offer_state.py        OfferResolution - OPEN/DECLINED/FULFILLED/EXHAUSTED/MOOT
│
├── tools/                    the orchestrator's own tools (registry.ALL_TOOLS)
│   ├── registry.py           every tool registered here
│   ├── campaign_data.py      set_campaign_spec, field validation, offer resolutions
│   ├── suggestions.py        present_options - chip widgets, one ask per turn
│   ├── product.py            analyze_product
│   ├── competitor.py         analyze_competitors + competitor-list curation
│   ├── creatives.py          fetch_competitor_creatives (consent-gated)
│   ├── research.py           research helpers
│   ├── location.py           manage_targeting_locations, confirm_location
│   ├── accounts.py           ad-account / page selection
│   ├── asset_manage.py       manage_assets (uploads)
│   ├── craft.py              craft-panel emits
│   ├── summary.py            show_campaign_summary
│   └── launch.py             launch_campaign (consent-gated)
│
├── agents/                   sub-agents - each has its own loop and AGENT.md
│   ├── product/              Product Analyst: scrape, profile, assets, competitor discovery
│   │   ├── adapters/         playwright_adapter, web_fetch_adapter, html_parser
│   │   ├── prompts/          product_profile.txt, product_assets.txt
│   │   └── tools/            scrape/ (tool, profile, assets, receipts) + comp_discovery
│   ├── location/             Location Agent: geo discovery, geocoding, platform mapping
│   │   └── tools/            discover_neighborhoods, geocode_recommendations, edit_locations
│   ├── creative_essence/     Essence Analyst: typed Essence off competitor creatives
│   ├── vision/               Vision Analyst: image review + selection
│   ├── summary/              Profile Writer: the streamed product summary
│   ├── campaign/             scaffold only (README)
│   └── optimization/         scaffold only (README)
│
├── creative_intelligence/    competitor-creative ingest - no LLM loop of its own
│   ├── library.py            fetch → dedupe → verify → gate → store orchestration
│   ├── freshness.py          when a stored record is too old to serve
│   ├── models.py             Competitor / Creative / Essence
│   ├── dedup.py, phash.py    3-tier dedup (creative id, exact hash, perceptual)
│   ├── verify.py             asset verification
│   ├── taxonomy.py           the category relevance gate (fails closed)
│   ├── enrich.py             the EssenceAnalyst seam
│   ├── sweep.py              repair sweep
│   └── scrapecreators.py     the competitor-ad source (Meta Ad Library scrape)
│
├── adapters/                 external platform clients
│   ├── meta/                 client, accounts
│   ├── google/               client, accounts, maps
│   └── connections.py
│
└── services/
    └── product_service.py    saves + restores the product and campaign draft (via db.py)
```

## The journey engine (`workflow.py`)

The funnel is a typed registry, all in `workflow.py` unless noted:

- **`AdzumpContext`** (`workflow.py:45`) - one frozen read of the session per
  turn: product, spec, competitor names, offer resolutions, the open
  elicitation's field, ask counts. Built ONCE (`from_session`,
  `workflow.py:82`) so every gate reads the same facts.
- **`Step`** (`workflow.py:140`) - `name`, `requires` (dependencies), `done`
  (predicate over the context), `prescribe` (the exact instruction the model
  gets when this step is next). The step registry is `NEW_CAMPAIGN`
  (`workflow.py:441`): product, location, platform, target_areas,
  competitive_analysis, competitor_creatives, duration, budget,
  parent_account, account, fb_page, instagram.
- **`missing_list(journey, actx)`** (`workflow.py:162`) - the ordered
  still-missing lines; its first entry is the turn's Next action.
- **Offers** are typed resolutions (`OfferResolution` in
  `models/offer_state.py`: OPEN / DECLINED / FULFILLED / EXHAUSTED / MOOT)
  computed by the `*_offer_resolution` functions in `tools/campaign_data.py`
  (creatives: `creatives_offer_resolution`; also analysis, instagram) -
  rationale as data, so the prescription, the gates, and the turn log can
  never disagree. The creatives offer is COVERAGE-based: fulfilled only while
  every named competitor carries a fetch result, so a competitor added after
  the fetch re-opens it.

## Capture rails (code owns state, the model never "remembers")

All rails live in `agent.py`; the widgets they capture from are emitted by
`tools/suggestions.py` (`present_options`).

- **Layer 1 - tagged answers**: every field-tagged `present_options` option
  carries an `answer`; a chip click (or exact-match reply) is captured by
  `_capture_tagged_answer` BEFORE the LLM runs. `answer: null` is a declared
  fall-through; a missing key is refused at the tool boundary.
- **Layer 2 - steered re-select**: a typed reply that matches a pending ask's
  candidates steers the model to store it via `set_campaign_spec` (validated
  against `field_candidates` - anti-invention).
- **Prose declines** (`_record_prose_decline`) and **elicitation resume**
  (`_resume_elicitation_section`) handle "no thanks" and stale widgets; a rail
  older than `STALE_RAIL_TURNS` user turns steps aside.
- Every turn emits a structured **`turn_decision`** log line
  (`observability.py`): prescription, missing list, captures, offer states -
  the first thing to read when the flow misbehaves.

## Hard gates (code, not prompt)

- `launch_campaign` (`tools/launch.py`) and `fetch_competitor_creatives`
  (`tools/creatives.py`) are consent-gated: the user's latest message (or a
  STORED accepted offer - the "stored-ok" exception) must be a clear
  go-ahead; the tool refuses otherwise.
- `present_options` (`tools/suggestions.py`) owns the whole assistant turn
  for a discrete-choice ask - one question per turn, never free-typed option
  lists.
- The competitor list is user-REVIEWED before ad-library credits are spent
  (the review checkpoint between analysis and the creatives fetch).
- Every-turn autosave writes the product + campaign draft to MySQL
  (`services/product_service.py`); `campaign.status` mirrors the launch
  flag, never asserts it.

## Provider configuration

| Setting | Default | Notes |
|---|---|---|
| `ADZUMP_PROVIDER` | `deepseek` | the orchestrator loop; falls back to `LLM_PROVIDER` |
| `AGENT_MODEL_TIER` / `MAX_AGENT_TURNS` / `AGENT_MAX_TOKENS` | shared settings | see `app/config.py` |

Sub-agents pin their own providers (research = Anthropic Sonnet for the server
web_search tool; vision + essence = DeepSeek vision; location = DeepSeek).

## HTTP surface (`router.py`)

Deliberately small: `POST /chat` (SSE stream) + the common session routes
(`core/base_router.py`) + the folded-in location search route
(`agents/location/search_router.py`) so `main.py` mounts ONE router.

The product library reads (`products_router.py`, folded in the same way) serve
the UI without the LLM, scoped to the caller's client:
`GET /products`, `GET /products/{id}`, `DELETE /products/{id}` (cascades flows,
competitors, creatives), `GET /products/{id}/competitors` (pending rows included),
`GET /products/{id}/creatives?competitor_id=` (grouped by competitor). No create
routes: products and competitors come from the chat's analysis.

## Sub-agent routing table

| Tool | Sub-agent | Doc |
|---|---|---|
| `analyze_product`, `analyze_competitors` | Product Analyst | `agents/product/AGENT.md` |
| `manage_targeting_locations`, `confirm_location` | Location Agent | `agents/location/AGENT.md` |
| `fetch_competitor_creatives` | creative_intelligence ingest + Essence Analyst | `agents/creative_essence/AGENT.md` |
| `manage_assets` (uploads) | Vision Analyst (review-each) | `agents/vision/AGENT.md` |
| (inside the scrape) | Profile Writer, Vision Analyst (select) | `agents/summary/AGENT.md`, `agents/vision/AGENT.md` |

## Testing

| File | Covers |
|---|---|
| `tests/agents/adzump/test_workflow.py` | journey steps, missing_list ordering, prescriptions, offer gating |
| `tests/agents/adzump/tools/test_campaign_data.py` | field apply/validation, dependents clearing, offer resolutions, fetch steer |
| `tests/agents/adzump/test_agent.py` | capture rails, resume, turn reminder |
| `tests/agents/adzump/tools/` | one file per tool module |

Run: `python -m unittest discover -s tests/agents/adzump`.

## Design decisions

- **Journey engine over an if-chain**: steps are data - dependencies,
  done-predicates, and prescriptions live in one registry a test can iterate.
- **Rationale as data**: offers resolve to a typed reason, logged per turn -
  "why did it ask X" is answerable from one `turn_decision` line.
- **Code captures, the model acknowledges**: chip answers are stored by the
  rail before the LLM ever runs; prompt-only capture rules drift.
- **Sub-agents only for genuinely different jobs** - the orchestrator's loop
  IS the interpreter for campaign construction; research/geo/vision are
  different jobs with their own loops. No sub-agent exists for a feature this
  loop already owns.
- **No Custom chip**: chip questions end with "or type your own"; typed
  values are first-class via the layer-2 rail.
