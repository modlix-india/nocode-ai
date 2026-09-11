# Product Analyst (`ProductAgent`)

> **Status: implemented; this doc written 2026-09-11** - describes the code as it is.

## Purpose

Deep business understanding + competitor discovery for one business URL. Given
`cityville.in` it scrapes the site (with screenshot), derives the competitive
dimensions, runs buyer-minded web searches, judges and VERIFIES candidate
competitors, and returns one structured JSON analysis (`business` +
`competitive`). Its output becomes `product_data` / `competitor_analysis` in the
session and is persisted to AISuggestedData.

It is the research half of the funnel; the orchestrator (Adzump) routes to it
and never does the research itself.

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│  Adzump orchestrator (LLM)                                         │
│  analyze_product / analyze_competitors tool call                   │
└──────────────────────────────┬─────────────────────────────────────┘
                               │ tools/product.py · tools/competitor.py
                               ▼
┌────────────────────────────────────────────────────────────────────┐
│  ProductAgent.analyze(url, ..., user_message, enforce_verified_…)  │
│  sub-session → self.run() on Claude Sonnet 4.6, ≤25 turns          │
│                                                                    │
│  The loop's tools (tools/__init__.py :: PRODUCT_TOOLS):            │
│    ├─ scrape_url            Playwright render + screenshot (ours)  │
│    ├─ web_search            Anthropic SERVER-side search (≤10)     │
│    ├─ web_fetch             Anthropic SERVER-side text fetch (≤10) │
│    ├─ extract_candidates    pool/dedupe search hits → fact table   │
│    └─ fetch_candidates      verify picked IDs → evidence per ID    │
│                                                                    │
│  Helper LLMs spawned from inside the scrape tool:                  │
│    ├─ SummaryAgent (Profile Writer)   streams the profile summary  │
│    └─ VisionAnalyst (selector)        picks logo + creative images │
│                                                                    │
│  Final turn = ONE fenced JSON block (business + competitive)       │
└────────────────────────────────────────────────────────────────────┘
```

### File layout

```
app/agents/adzump/agents/product/
├── agent.py            ProductAgent (BaseAgent) + get_product_agent()
│                       + _PassthroughEventStream + the verified-output
│                       bounce/scrub (_discovery_violations, _scrub_unverified)
├── context.py          ANALYST_SYSTEM_PROMPT (workflow, budgets, judgment
│                       rules, output contract) + build_product_context()
├── models.py           SiteLink/SiteImage, LogoPick/CreativeRole/ProductAssets,
│                       PageContent/ScrapeResult, AssetRequirements,
│                       AnalysisOutput (analyze()'s return)
├── product_assets.py   candidate prefilter + thumbnail fetch + VisionAnalyst
│                       pick + persistence of picked assets
├── scrape_stages.py    per-stage progress narration for the scrape card
├── prompts/            product_profile.txt, product_assets.txt
├── adapters/           playwright_adapter (render+screenshot), web_fetch_adapter,
│                       html_parser (links/images/structured-data extraction)
└── tools/
    ├── __init__.py     PRODUCT_TOOLS registry incl. the two Anthropic
    │                   builtin_spec tools (web_search, web_fetch)
    ├── comp_discovery.py  extract_candidates + fetch_candidates
    └── scrape/         scrape_url tool (tool.py), profile.py (SummaryAgent
                        call), assets.py (VisionAnalyst call), receipts.py
```

## Provider configuration

| Constant | Value | Why |
|---|---|---|
| `ANALYST_PROVIDER` | `"anthropic"` | pinned - the server-side `web_search`/`web_fetch` builtin tools only exist on Anthropic |
| `ANALYST_MODEL_OVERRIDE` | `anthropic:claude-sonnet-4-6` | 25-turn research flow needs the strong tier |
| `ANALYST_MAX_TURNS` | `25` | 1 scrape + 7 searches + extract + fetch + final JSON + slack |
| `ANALYST_MAX_TOKENS` | `16384` | the final JSON is 3-5K tokens; 4K truncated mid-JSON (stop_reason=max_tokens) |
| `context_management` | clear_tool_uses at 100k input tokens, `exclude_tools=["web_search"]` | search results ARE the judgment evidence; clearing them mid-run starved the final turns (B1, 2026-09-02) |

All constants live at the top of `agent.py`.

## The two scopes - one agent, caller-controlled

`analyze()` takes the instruction as `user_message`; the caller picks the scope:

| Caller | Scope | `enforce_verified_competitors` |
|---|---|---|
| `tools/product.py` (`analyze_product`) | profile only ("do NOT search for competitors") | off |
| `tools/competitor.py` (`analyze_competitors`) | full discovery, or targeted add/set_url | on for full discovery |

## The verified-competitors contract (full discovery only)

Server-side search returns results inline, so the model can SEE plausible
competitors after two searches and write the final JSON from memory - skipping
the extract/fetch pipeline and hand-typing URLs (live 2026-09-08). Code gives
the pipeline rule teeth (`agent.py`):

1. `_discovery_violations()` - every competitor entry must cite a verified
   `competitor_id` from `fetch_candidates` evidence (or at least carry no
   model-typed URL); an EMPTY list only counts if `extract_candidates` ran.
2. One bounce: the run is re-entered with `_UNVERIFIED_BOUNCE_MSG` telling the
   model to fix ONLY the flagged entries.
3. Last resort: `_scrub_unverified()` strips model-typed URLs/citations
   (entries ship honestly link-less - creatives still fetch by name); an empty
   list produced without ever opening the pipeline DISCARDS the competitive
   section (a fake "found nobody" would silently settle the creatives offer).

Why link honesty matters: a wrong URL poisons the SHARED creative library key
(a hijacked `cityville.in` join got Valmark Cityville zero ads, 2026-09-10).

## How a full run flows (per the system prompt in context.py)

1. `scrape_url` once (rendered DOM + screenshot) then `web_fetch` on the same
   URL (raw HTML: JSON-LD, og:, noscript).
2. Derive 6 dimensions (offering, geography, price tier, buyer, differentiator,
   business_scale).
3. 7 `web_search` queries - 5 buyer-minded discovery + 2 review/comparison.
4. `extract_candidates()` once → fact table (ID, name, host, seen-in count,
   aggregator flag). The MODEL judges every row (one-line verdict each,
   asymmetric price bands, corridor-not-road geography).
5. `fetch_candidates(ids)` on the 6-8 picks → re-judge on fetched evidence,
   judge each official URL (sibling-project trap; null beats wrong).
6. Final fenced JSON: `business` + `competitive` (competitors cite evidence
   IDs, `url: null`).

During the scrape, `scrape_stages.py` narrates the card and two helper agents
run: `SummaryAgent.summarize()` (streams the profile into the craft panel) and
`VisionAnalyst.pick()` (logo + creative selection) - see their own AGENT.md
files.

## Event streaming

`_PassthroughEventStream` (agent.py) forwards `thinking` / `tool_start` /
`tool_update` / `tool_result` / `craft*` / `data` / `agent_*` to the parent
stream so the analyst's work nests inside its AgentCard; it DROPS `text`
(the final JSON must not leak to chat), `done`, and `error` (the parent owns
those). Cancellation delegates to the parent, so a user cancel propagates in.

## Failure handling

- Unparseable/absent final JSON → `_build_minimal_result()` salvages whatever
  the sub-session accumulated (screenshot, raw search snippets as notes) with
  an honest "Automated research failed" summary.
- The tool wrappers convert exceptions into structured ToolResults; there is
  no deterministic fallback analysis pipeline.

## Return type

`AnalysisOutput` (models.py): `product` (the JSON's `business` section),
`competitive`, `notes`, `raw_text`, and `asset_requirements` (what the asset
picker could NOT satisfy from the site - drives the upload elicitation;
popped off the sub-session context, never stored in product_data).

## Testing

| File | Covers |
|---|---|
| `tests/agents/adzump/agents/product/test_agent.py` | analyze() wiring, verified-output contract |
| `tests/agents/adzump/agents/product/test_product_assets.py` | candidate prefilter + pick resolution + persistence |
| `tests/agents/adzump/agents/product/tools/test_comp_discovery.py` | extract/fetch candidates: pooling, dedupe, aggregator-follow, official-URL options |
| `tests/agents/adzump/agents/product/adapters/test_html_image_parse.py` | html_parser image/link extraction |

Run: `python -m unittest discover -s tests/agents/adzump`.

## Design decisions

- **The model is the judge; tools only gather facts.** `extract_candidates`
  does NO scoring, `fetch_candidates` does NO picking - judgment (and its
  audit trail) lives in the model's reasoning, per the URL-judging-lives-in-
  the-researcher rule (no standalone cheap-model judge).
- **Server-side search/fetch over custom scrapers** for discovery: no scraping
  infra for search results, citations included; `scrape_url` (Playwright)
  stays for the one page that needs a rendered screenshot.
- **Contract enforcement is code, not prompt-only** - the bounce/scrub in
  `analyze()` exists because prompt rules drift under model pressure.
- **Sub-session with selective context sharing**: the agent gets references to
  `product_data` / `product_profile` / `_research_state` only - writes
  propagate to the parent, but campaign state is unreachable.
