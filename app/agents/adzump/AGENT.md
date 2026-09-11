# Adzump Orchestrator (`AdzumpAgent`)

> **Status: implemented; this doc written 2026-09-11** - describes the code as
> it is. Sub-agents have their own AGENT.md files (see the routing table).

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
│    CampaignContext.from_session → missing_list(NEW_CAMPAIGN, cctx)   │
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

## The journey engine (`workflow.py`)

The old `next_action` if-chain is gone (rework PR-3a/3b). The funnel is a
typed registry:

- **`CampaignContext`** - one frozen read of the session per turn: product,
  spec, competitor names, offer resolutions, the open elicitation's field,
  ask counts. Built ONCE (`from_session`) so every gate reads the same facts.
- **`Step`** - `name`, `requires` (dependencies), `done` (predicate over the
  context), `prescribe` (the exact instruction the model gets when this step
  is next). Steps: product, location, platform, target_areas,
  competitive_analysis, competitor_creatives, duration, budget,
  parent_account, account, fb_page, instagram.
- **`missing_list(journey, cctx)`** - the ordered still-missing lines; its
  first entry is the turn's Next action.
- **Offers** are typed resolutions (`OfferResolution`: OPEN / DECLINED /
  FULFILLED / EXHAUSTED / MOOT) computed in `tools/campaign_data.py` -
  rationale as data, so the prescription, the gates, and the turn log can
  never disagree. The creatives offer is COVERAGE-based: fulfilled only while
  every named competitor carries a fetch result, so a competitor added after
  the fetch re-opens it (live 2026-09-11).

## Capture rails (code owns state, the model never "remembers")

- **Layer 1 - tagged answers**: every field-tagged `present_options` option
  carries an `answer`; a chip click (or exact-match reply) is captured by
  `_capture_tagged_answer` BEFORE the LLM runs. `answer: null` is a declared
  fall-through; a missing key is refused at the tool boundary.
- **Layer 2 - steered re-select**: a typed reply that matches a pending ask's
  candidates steers the model to store it via `set_campaign_spec` (validated
  against `field_candidates` - anti-invention).
- **Prose declines** (`_record_prose_decline`) and **elicitation resume**
  (`_resume_elicitation_section`) handle "no thanks" and stale widgets; a rail
  older than `STALE_RAIL_TURNS` user turns steps aside (R6/S1-11).
- Every turn emits a structured **`turn_decision`** log line
  (`observability.py`): prescription, missing list, captures, offer states -
  the first thing to read when the flow misbehaves.

## Hard gates (code, not prompt)

- `launch_campaign` and `fetch_competitor_creatives` are consent-gated: the
  user's latest message (or a STORED accepted offer - the "stored-ok"
  exception) must be a clear go-ahead; the tool refuses otherwise.
- One question-asking tool per turn (`elicitation_break`); questions go
  through `present_options`, never free-typed option lists.
- The competitor list is user-REVIEWED before ad-library credits are spent
  (the review checkpoint between analysis and the creatives fetch).
- Every-turn autosave writes the campaign draft to AISuggestedData
  (`services/business_storage.py`); `campaign.status` mirrors the launch
  flag, never asserts it.

## Provider configuration

| Setting | Default | Notes |
|---|---|---|
| `ADZUMP_PROVIDER` | `deepseek` | the orchestrator loop (Kailash 2026-09-08); falls back to `LLM_PROVIDER` |
| `AGENT_MODEL_TIER` / `MAX_AGENT_TURNS` / `AGENT_MAX_TOKENS` | shared settings | see `app/config.py` |

Sub-agents pin their own providers (research = Anthropic Sonnet for the server
web_search tool; vision + essence = DeepSeek vision; location = DeepSeek).

## HTTP surface (`router.py`)

Deliberately small: `POST /chat` (SSE stream) + the common session routes
(`core/base_router.py`) + the folded-in location search route
(`agents/location/search_router.py`) so `main.py` mounts ONE router.

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

- **Journey engine over an if-chain** (PR-3a/3b): steps are data - dependencies,
  done-predicates, and prescriptions live in one registry a test can iterate.
- **Rationale as data**: offers resolve to a typed reason, logged per turn -
  "why did it ask X" is answerable from one `turn_decision` line.
- **Code captures, the model acknowledges**: chip answers are stored by the
  rail before the LLM ever runs; prompt-only capture rules drift (the
  historic capture-ack incident).
- **Sub-agents only for genuinely different jobs** - the orchestrator's loop
  IS the interpreter for campaign construction; research/geo/vision are
  different jobs with their own loops. No sub-agent exists for a feature this
  loop already owns.
- **The Custom chip is dead (D13)**: chip questions end with "or type your
  own"; typed values are first-class via the layer-2 rail.
