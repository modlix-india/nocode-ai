# Keyword Research Agent

Brand + generic keyword research for **Google Search** campaign creation. This doc
covers the whole path — from the main adzump agent down to the keyword agent's
tool loop — what each piece does, how it differs from the legacy Adzump-AI flow,
how to read the run logs, and the craft-panel issue we hit and fixed.

---

## 1. The core idea

The legacy flow was a **fixed, linear service**: one function generated seeds, the
next fetched suggestions, the next selected positives, the next generated negatives —
each a separate LLM call against a static prompt file. The model never saw the real
Google data while deciding; it couldn't adapt, re-query, or catch when its seeds had
drifted into the wrong product category.

The new flow is an **agentic ReAct loop**. One `KeywordResearchAgent` *drives* the
research through tools, reasoning over **real Planner volume/competition as it goes**.
A small base prompt plus a per-turn "phase" prompt keeps each step as focused as a
dedicated prompt, while the agent still decides what to do next and can loop. A
business-agnostic **context layer** (the offering taxonomy) anchors every run so it
works for *any* business without hardcoded category rules.

> **One line for a reviewer:** we replaced a blind, fixed pipeline with a data-aware
> agent + a derived business-context layer + deterministic safety gates.

---

## 2. End-to-end flow (main agent → keywords → panel)

```mermaid
sequenceDiagram
    actor User
    participant Main as Main Agent (adzump)
    participant CC as prepare_campaign_review (tool)
    participant CA as CampaignAgent
    participant KR as keyword_research (tool)
    participant Tax as offering taxonomy
    participant KA as KeywordResearchAgent
    participant Panel as Review Panel (craft)

    User->>Main: confirms summary → "Yes, proceed"
    Main->>CC: prepare_campaign_review()  (no params)
    CC->>CA: create(spec, product_data, craft_id = campaign_<sid>)
    CA->>KR: keyword_research(keyword_type="both")
    KR->>Tax: derive_offering_taxonomy(product)  (1 LLM call, cached)
    Tax-->>KR: core_terms / sibling_categories / is_location_specific
    par brand & generic run in parallel
        KR->>KA: research(type=brand,  taxonomy, geo, location, …)
        KR->>KA: research(type=generic, taxonomy, geo, location, …)
    end
    KA-->>KR: KeywordSet(positives, negatives)
    KR->>Panel: emit_campaign_craft(craft_id)
    KR-->>CA: result bundle
    CA-->>CC: keyword_research result
    CC-->>Main: elicited=multi → loop pauses, review open
    Note over User,Panel: add / edit / delete via keyword_update (fast path, no agent turn)
    User->>Main: "launch" → agent resumes, reads the edited set
```

**Why three agents?** Each layer has one job and stays swappable:

| Layer | Responsibility | Lives in |
|---|---|---|
| **Main agent** (`adzump`) | Collects + confirms campaign details; calls `prepare_campaign_review` | `agents/adzump/agent.py` |
| **`prepare_campaign_review` tool** | Spawns the `CampaignAgent`, persists its result for launch | `tools/prepare_campaign_review.py` |
| **`CampaignAgent`** | Platform-agnostic shell — runs the chosen platform's creation tools (Google Search → `keyword_research`). New platforms/channels slot in here without touching keywords | `agents/campaign/agent.py` |
| **`keyword_research` tool** | Derives the taxonomy, resolves geo/location, runs brand + generic **in parallel**, emits the craft | `agents/campaign/tools/google/keyword_research.py` |
| **`KeywordResearchAgent`** | The agentic loop for ONE type (brand or generic) | `agents/campaign/google/keyword/agent.py` |

### Review & edit — the elicitation model

When `prepare_campaign_review` shows the panel it returns `elicited=True, elicit_expects="multi"` — the
same primitive the asset-upload step uses. The main-agent loop **pauses** there instead of
barrelling to launch; `_pending_elicitation` records that a review is open.

Panel edits (`add` / `edit` / `delete`) post to the chat endpoint, which hands them to a
**separate fast path** ([`router.py`](../../../../router.py) → `stream_keyword_widget` in
[`api.py`](../../api.py), which calls `update_keywords` in
[`keyword_update.py`](../../tools/google/keyword_update.py)) that mutates
`session_ctx["keyword_research"]` and re-emits the panel — **no LLM turn, nothing written to
chat history**. The agent isn't called per click.

The agent is called **once**, when the user sends a real message (e.g. "launch"): the elicitation
closes, `_resume_elicitation_section` steers it to acknowledge the edits and move to launch, and
`launch_campaign` reads the already-edited `keyword_research` from the session. So edits are honored
without the agent re-enumerating them. (See [`prepare_campaign_review.py`](../../../../tools/prepare_campaign_review.py)
and `agent.py._resume_elicitation_section`.)

### Why keyword edits diverge from location edits (a deliberate choice)

The location sub-agent made the **opposite** call. It has **no widget fast path** — every geo
edit (even a map click) runs through its own LLM loop (`LocationAgent.handle`), so the agent is
always the one driving. We kept a fast path for keywords. This is a real, deliberate divergence,
not an oversight — and it's worth writing down so a reviewer doesn't read it as inconsistency:

| | Location edits | Keyword edits (here) |
|---|---|---|
| **Path** | LLM-mediated — every edit → `handle()` | Fast path — widget → `keyword_update`, no LLM |
| **Agent aware?** | Always (it's driving) | Kept in sync by the elicitation above |
| **Cost** | 1 LLM call per edit | 0 LLM calls per edit |

Both are correct — the choice tracks **edit frequency and complexity**:

- **Location edits are few and complex.** Adding a city or a service area is a handful of
  deliberate actions, so an LLM turn per edit is affordable and keeps the agent authoritative.
- **Keyword edits are many and simple.** A user prunes a long list, adding/removing rows in
  quick succession. An LLM turn per click would be slow and costly for what is a plain set
  mutation, so we mutate `session_ctx["keyword_research"]` directly and re-render. The
  **elicitation** buys back the awareness the fast path gives up: the agent reads the
  fully-edited set **once**, on resume, instead of being pinged per row.

So the elicitation isn't a workaround — it's the piece that makes a 0-LLM-per-edit path safe:
the user edits freely and cheaply, and the agent still sees a correct, final set before launch.

**Honest flag for reviewers:** the two subsystems now differ in how panel edits flow — the
location agent deliberately deleted its widget protocol, `keyword_research` deliberately keeps
one. Neither is wrong for its own edit profile, but if the team wants a single pattern across
the product, that's a worthwhile conversation — and the edit-frequency difference above is the
ground to have it on.

---

## 3. Inside the keyword agent (one run = brand OR generic)

The agent's base prompt is small; **`build_turn_reminder` injects only the current
phase's prompt** based on progress (seed → select → negatives). So each turn reads
like a dedicated prompt, but a single agent drives and can loop.

```mermaid
flowchart TD
    A([research start]) --> B

    subgraph Context["injected once per run (build_dynamic_context)"]
      direction LR
      C1[OFFERING + CORE TERMS] --- C2[SIBLING CATEGORIES] --- C3[LOCATION + service areas]
    end

    B["SEED phase<br/>draft seeds anchored on CORE TERMS"] --> T1
    T1[["expand_keywords<br/>multi-source autosuggest → candidate pool"]] --> T2
    T2[["keyword_metrics<br/>score the FULL pool via Keyword Planner"]] --> D
    D["SELECT phase<br/>pick positives from the REAL scored data"] --> T3
    T3[["submit_positive_keywords<br/>validate vs candidate pool"]] --> E
    E["NEGATIVES phase<br/>reason exclusions from the business model"] --> T4
    T4[["submit_negative_keywords<br/>overlap-checked, volumes fetched"]] --> F
    F([KeywordSet → review panel])

    Context -.anchors every phase.-> B
    Context -.-> D
    Context -.-> E
```

**The amplification chain (why seed quality matters most):**
good seeds → good autosuggest expansion → more/better Planner ideas → more relevant
selections. Seeds are the richest prompt of the set for exactly this reason.

**The five tools** (thin wrappers; all judgment stays in the agent's reasoning):

| Tool | Does |
|---|---|
| `expand_keywords` | Fans seeds out across Google / Bing / DuckDuckGo autosuggest → real searched phrasings |
| `keyword_metrics` | Scores the pool through the Keyword Planner (real volume / competition / CPC) — the relevance gate. Also **recovers** clean candidates the expansion misses (below) |
| `fetch_more_candidates` | Pages through lower-volume scored candidates |
| `submit_positive_keywords` | Validates picks against the scored pool, records positives |
| `submit_negative_keywords` | Records negatives, fetches their volumes |

**LLM proposes, the tool layer disposes.** Every keyword the model emits passes
deterministic gates it can't skip: keyword normalisation + length, match-type/intent
coercion, **cross-business → PHRASE** enforcement (`models.py`), candidate-membership
on positives, and a **token-overlap drop** on negatives that collide with positives
(`tools.py`). A bad model turn can't produce an unsafe or self-conflicting set.

### Negative match types (phrase / broad — never exact)

Negatives exclude a *concept*, so the agent picks **phrase** or **broad**, never **exact**. Exact is
the narrowest — it blocks only the literal query and lets every longer variation leak through, so it
barely excludes anything. Google's own "running shoes" example ([match types][neg-mt]):

| Search query | neg **broad** `running shoes` | neg **phrase** `"running shoes"` | neg **exact** `[running shoes]` |
|---|:---:|:---:|:---:|
| `running shoes` | blocked | blocked | blocked |
| `blue running shoes` | blocked | blocked | **shows** |
| `shoes running` | blocked | shows | shows |
| `buy running shoes online` | blocked | blocked | **shows** |

So **phrase** (default) blocks the term plus anything containing it in order; **broad** blocks a
search with all the words in any order (multi-word concepts whose order varies). The schema
(`submit_negative_keywords`) and the validator (`_coerce_negative_match_type`, `models.py`) allow
only these two — a stray `exact` is coerced to phrase.

[neg-mt]: https://support.google.com/google-ads/answer/2453972

### Candidate recovery (why `keyword_metrics` calls two Planner APIs)

`generateKeywordIdeas` (the expansion) is powerful but has two failure modes: it caps how
many seeds we can send (the overflow is discarded) and it occasionally returns **duplicate-token
junk** (`prescription glasses prescription` instead of `prescription glasses`) — so a real head
term can be absent from the scored pool. `keyword_metrics` closes both gaps with a second,
cheaper API — `generateKeywordHistoricalMetrics` (exact scoring, **no** re-expansion, no
mangling) — over two feeds:

1. **Overflow** — the real autosuggest queries beyond the expansion cap (otherwise thrown away).
2. **De-mangled repairs** — for any idea with a repeated token, the order-preserving collapsed
   form (`_collapse_repeats`).

Both are scored exactly and kept **only if they have real volume**, then merged into the pool
(dedup, keep highest volume). Two safeguards make this incapable of corrupting data: every
recovered form is **validated against Google's own volume** (a bad collapse scores ~0 and is
dropped), and the **original is never dropped** — recovery only ever *adds* a clean candidate.
Misspellings are single tokens, never repeats, so they're never touched.

> **Deferred (pending review):** a mangled original and its clean twin both currently sit in
> the pool. We could drop the original **only when** its collapsed twin scored essentially the
> same volume (a strong "pure mangle" signal — `X X` 368k ≡ `X` 368k), while keeping both when
> volumes differ (`new york new york` ≠ `new york`). Safe de-clutter; left out of the first cut
> deliberately — implement after review if wanted.

### Why there's no critic / repair pass (considered, prototyped, removed)

We considered a **review-then-repair** pass over the selected set — and actually prototyped a
deterministic "floor" version — but **removed it**: a live trace showed it was solving a problem
the recovery step already solves. With a clean pool, the model selects the real head on its own
and skips the mangled twins; the prototype only **re-injected the mangled forms the model had
correctly avoided**. It turned out to overlap almost entirely with the de-mangle step above.

> **When an LLM critic would earn its place:** only if future live runs show the draft is
> genuinely and repeatedly inconsistent in a way the phase prompts can't fix — then a critic is
> justified with *evidence, not on spec*. A reviewer pass pays off when it grades many decisions
> at once; for a single keyword set on a clean pool, the model's own pass is enough.

---

## 4. The context layer — offering taxonomy

Product analysis doesn't persist a category/sibling taxonomy, so we **derive one once
per run** from the confirmed `product_data` (business_type, products/services, USPs,
summary) via a single balanced-tier LLM call (cached by an offering fingerprint,
fail-soft). It yields:

- **`core_terms`** — what the business actually sells (anchor every seed here)
- **`sibling_categories`** — adjacent same-industry things it does *not* sell (→ negatives)
- **`is_location_specific`** — local/regional vs national/online (drives location anchoring)
- **`sells_physical_products`** — a shippable retail/ecommerce product → adds **Amazon** product-intent autosuggest
- **`includes_informational_funnel`** — buyers research via how-to/educational content → adds **YouTube** autosuggest

The last two drive **`BusinessProfile.source_names()`** — which autosuggest surfaces `expand_keywords`
queries per business (web-search default: Google/Bing/DuckDuckGo; plus Amazon/YouTube when the signal
fits). Data-driven per run, so it works for any vertical without hardcoded rules.

This is what makes the agent **business-agnostic** — no hardcoded verticals, no
`business_scale` string-matching. An upsell-adjacent sibling (same buyer, adjacent
budget — e.g. *3 BHK villa* for a *3 BHK apartment* buyer) may be targeted as a
deliberate **cross-business PHRASE** positive; everything else sibling stays a negative.

---

## 5. Reading the run logs (the funnel)

Each type emits a clean, greppable funnel. To follow one type: `grep "type=generic"`.

A real generic run (Warby Parker, US), one line per stage:

```
kw_expand type=generic seeds=60 autosuggest=445 pool=200 overflow=281
keyword_planner: scoring 200 candidates in 14 Planner call(s)
kw_metrics type=generic sent=200 planner_ideas=4793 recovered=123 scored_pool=600
kw_submit_positive type=generic submitted=15 kept=9 dropped=6
kw_submit_negative type=generic submitted=14 kept=14 dropped=0
keyword_research done type=generic positives=9 negatives=14
```

| Log line | Read it as |
|---|---|
| `kw_expand … pool=… overflow=…` | `pool` = top slice sent to the Planner's expansion; `overflow` = real autosuggest queries beyond the cap, scored later (not discarded) |
| `keyword_planner: scoring N … M call(s)` | the pool reaches `generateKeywordIdeas`; `M = ceil(N / 15)` calls |
| `kw_metrics … planner_ideas=… recovered=… scored_pool=…` | `planner_ideas` = what the expansion returned; **`recovered`** = clean terms rescued via historical metrics from the overflow + de-mangled repairs (kept only if they have real volume — see §3); `scored_pool` = stored for selection |
| `kw_submit_positive/negative … submitted/kept/dropped` | model proposed → validated & kept → dropped (not in the scored pool / dupe / overlap) |
| `keyword_research done` | final counts that reach the panel |

> On this run `recovered=123` is the de-mangle/overflow step doing its job — that's how the clean
> `prescription glasses` (368k) reached the pool and got selected, instead of only the mangled
> `prescription glasses prescription` the expansion returned. (There is **no** critic/repair line
> in the funnel — that was considered and removed; see §3.)

> Tip: the hundreds of `httpx … 200 OK` lines are httpx's own logger. Set
> `logging.getLogger("httpx").setLevel(logging.WARNING)` to collapse the log to just
> the `kw_*` funnel.

---

## 6. Old vs new at a glance

| | Legacy (Adzump-AI `GoogleKeywordService`) | New (`KeywordResearchAgent`) |
|---|---|---|
| Control flow | Fixed linear service: seed → suggest → select → negatives | Agentic ReAct loop; agent decides each step, can re-query |
| Prompts | One static `.txt` per phase | Small base + **per-turn phase prompt** injected by progress |
| Sees real Planner data while deciding? | No — selection ran after, blind | **Yes** — reasons over live volume/competition; catches category drift |
| Expansion | Google Ads suggestions | **Multi-source autosuggest** (Google / Bing / DuckDuckGo) |
| Pool → Planner | n/a | **Full pool** scored (the model's list augments, never replaces) |
| Business fit | Hardcoded / `business_scale` rules | **Derived offering taxonomy** — business-agnostic |
| Negatives | Generated separately | **Reasoned from the business model + the chosen positives** (no pool-mining) |
| Safety | In-prompt | **Deterministic gates** in the model + tool layer |
| Brand + generic | Sequential | **Parallel**, independent (one failing still returns the other) |

---

## 7. File map

```
agents/campaign/google/keyword/
├── agent.py            KeywordResearchAgent — the ReAct loop + per-turn phase injection
├── tools.py            the 5 tools + deterministic gates (overlap, membership, dedup)
├── taxonomy.py         derive_offering_taxonomy — the business-agnostic context layer
├── models.py           KeywordSet / OptimizedKeyword / NegativeKeyword + validators
├── constants.py        pool/seed/selection size knobs (see below)
└── context.py          all prompt text: BASE system prompt, the SEED/SELECT/NEGATIVES
                        phase templates, and the typed (phase, type) → prompt registry
                        (validated complete at import)

agents/campaign/                     CampaignAgent shell + keyword_research orchestrator tool
adapters/autosuggest.py              multi-source autosuggest
adapters/google/keyword_planner.py   Keyword Planner (generateKeywordIdeas), chunked + breaker
tools/prepare_campaign_review.py     main-agent entry that spawns the CampaignAgent
```

## 8. Tuning knobs (`constants.py`)

| Constant | Value | Meaning |
|---|---|---|
| `MAX_SEEDS` | 80 | seeds generated per type |
| `MAX_SEEDS_TO_EXPAND` | 30 | top seeds fanned out to autosuggest |
| `MAX_EXPANSION_CANDIDATES` | 200 | pool sent to the Planner (caps API calls) |
| `MAX_STORED_CANDIDATES` | 600 | scored ideas kept (Planner expands beyond input) |
| `TARGET_POSITIVE_COUNT` | 30 | positives target per type |
| `MAX_NEGATIVE_COUNT` | 40 | negatives kept per type |
| `_SEED_CHUNK_SIZE` (planner) | 15 | candidates per Planner call → calls = ⌈pool / 15⌉ |

---

## 9. LLM provider

The agent's **tool-use loop** runs on **OpenAI** today (`PROVIDER = "openai"` in `agent.py`)
through one abstraction — `app/services/llm_provider.py` → `get_llm_provider(name)`, an
`LLMProvider` ABC with a uniform tool-calling + streaming interface, implemented by
`AnthropicProvider`, `OpenAIProvider`, and `DeepSeekProvider`. The loop talks
to the **interface, never a vendor SDK**, so it switches at config level: set `PROVIDER` to
`"anthropic"` / `"deepseek"` and the `balanced` tier maps to each provider's model via config
(`CLAUDE_SONNET`, etc.). The target must support tool/function calling.

**The taxonomy step is the exception** — `taxonomy.py` makes a direct **AsyncOpenAI one-shot**
call in JSON mode (the sanctioned pattern for a self-contained inference), so it is
**OpenAI-only** and independent of `PROVIDER`.

**Billing for the one-shot.** A one-shot bypasses the loop, so it isn't auto-tracked. It's
still billed **per-agent** via `record_oneshot_usage` (core `session.py`), which records to the
currently-running agent's session with `record_token_usage` — the **same DB path the loop
uses**. `BaseAgent.run` publishes that session through the `current_session` contextvar (next to
`current_agent_id`), so no session is threaded through tool contexts. The taxonomy call is thus
attributed to the agent that invoked it (`campaign`), not dropped or lumped onto the main agent.

---

## 10. Craft-panel focus (the two-craft rule)

This flow uses two crafts — the setup craft (`adzump_<session>`) and the campaign
keyword-review craft (`campaign_<session>`). To stop a trailing setup re-emit from
stealing the panel once `prepare_campaign_review` has opened the review:

- **UI** (`nocode-ui/.../Prompt/LazyPrompt.tsx`): a craft surfaces the panel only the
  first time its id is seen (`seenCraftIds` ref); later re-emits of a known craft update
  content in place without stealing focus. Generalizes to every multi-craft stage.
- **Backend** (`agent.py._on_loop_complete`): once `prepare_campaign_review` has begun
  (`campaign_craft_id` set), the end-of-turn hook stops re-emitting the setup craft.
