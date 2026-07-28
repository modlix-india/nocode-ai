# Meta Detailed Targeting Subsystem

> **Status: active** - documentation updated to match current code implementation.

## Purpose

Discovers, maps, filters, and validates detailed targeting segments (Interests, Behaviors, and Demographics) for Meta Ads campaigns. For a B2B SaaS product, it finds specific software platforms, job titles, and employers; for a consumer brand, it finds lifestyle hobbies and life events. It heavily curates options from the Meta Graph API and strictly returns valid, active Meta segment IDs that are natively required for Ad Set creation.

## Architecture

The system follows a **sub-agent orchestration** discipline. The main Adzump orchestrator does not perform granular segment searches. Instead, it delegates to the `DetailedTargetingAgent` via the `suggest_meta_targeting` tool. 

The `DetailedTargetingAgent` spins up its own focused LLM tool-use loop. The prompt carries the overarching business profile, campaign objective, and current targeting state. The model then acts by orchestrating a sequence of API calls using **5 discovery/validation tools** plus `delete_targeting_segment` (6 tools total registered on the agent).

```text
┌──────────────────────────────────────────────────────────────────┐
│  Adzump orchestrator (LLM)                                       │
│  "User wants detailed targeting strategy" → route                │
│  call suggest_meta_targeting(ad_account_id=..., user_query=...)  │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  DetailedTargetingAgent.recommend()                              │
│  Sets up isolated sub-session → self.run(...)                    │
│                                                                  │
│  The loop's LLM executes a multi-step sequence using 5 core tools:│
│    ├─ fetch_interests                (search + suggestions)      │
│    ├─ fetch_behaviors                (catalog browse + search)   │
│    ├─ fetch_demographics             (fixed catalog browse)      │
│    ├─ search_professional_demographics(job titles/employers)     │
│    └─ validate_targeting             (FINAL: selected_ids)       │
│    (plus delete_targeting_segment for conversational deletes)   │
│                                                                  │
│  Fetched candidates are stashed in Python `_candidate_pool`.     │
│  Validated state is stashed into session context and emitted     │
│  to the UI via the `targeting_manager` craft block.              │
└──────────────────────────────────────────────────────────────────┘
```

**Why this matters:** The main orchestrator doesn't get bloated by handling Meta Graph API specifics, deduplication, or audience size limits. The sub-agent isolates the complexity. The LLM is forced to define a `<Strategy>` block *first*, then fetches diverse candidate segments, and ends by calling `validate_targeting(selected_ids=[...])` which discards inactive IDs via Meta's `/targetingvalidation` endpoint.

### File Layout

```text
app/agents/adzump/
├── adapters/meta/
│   └── targeting_adapter.py              Low-level Meta Graph API HTTP calls (search, browse, validate)
├── agents/meta_detailed_targeting/
│   ├── agent.py                          DetailedTargetingAgent (BaseAgent) + .recommend()
│   ├── context.py                        System prompt & rules for DetailedTargetingAgent
│   ├── models.py                         Pydantic models (TargetingEntity, MetaTargetingSuggestionResult)
│   ├── subagent_event_stream.py          MetaPassthroughEventStream (UI event wrapper)
│   ├── targeting_router.py               REST API endpoints for UI chip search, add, and delete
│   ├── AGENT.md                          This file
│   └── tools/
│       ├── targeting_tools.py            The 5 inner loop LLM tools (fetch_*, search_*, validate_targeting)
│       └── detailed_targeting_tool.py    The outer orchestrator tools (suggest_meta_targeting, delete_*)
```

---

## The Sub-Agent Tool Loop (Discovery & Validation)

When the orchestrator delegates to the sub-agent (via `suggest_meta_targeting`), the `DetailedTargetingAgent` takes over in an isolated loop with access to candidate discovery and validation tools.

### 1. The Strategy Requirement
Before calling any tools, the LLM is explicitly instructed by `context.py` to output a short `<Strategy>` block defining the buyer persona and search rationale. The `MetaPassthroughEventStream` intercepts this text and forwards it directly to the UI's chat window, keeping the user informed of the AI's logic before API calls begin.

### 2. Candidate Discovery & Pool Stashing (`_candidate_pool`)
The LLM selects seeds (e.g. brand names, job titles) and passes them to the fetch tools. Each tool uses a category-optimized Meta Graph API strategy:
- **`fetch_interests`**: Runs a parallel keyword search per seed (`GET /{account_id}/targetingsearch?q={seed}&limit_type=interests`), then executes a batched `targetingsuggestions` expansion query.
- **`fetch_behaviors`**: Combines a full `targetingbrowse` (`limit_type=behaviors`) of the behavior catalog with parallel keyword searches.
- **`fetch_demographics`**: Browses fixed demographic catalogs (`DEMOGRAPHIC_FIXED_SUBTYPES`: `life_events`, `family_statuses`, `income`, `industries`, `education_statuses`) in parallel without needing seeds (~99 total entries).
- **`search_professional_demographics`**: Searches open demographic databases (`DEMOGRAPHIC_SEARCHABLE_SUBTYPES`: `work_positions`, `work_employers`, `education_majors`) using keyword seeds.

**Candidate Pool Stashing:** Whenever candidates are fetched, Python calls `_stash_candidates()` to cache full `TargetingEntity` objects in `session_context["_candidate_pool"]` indexed by entity ID. This preserves exact Meta metadata (`name`, `type`, `audience_size`) in Python memory.

*Note: Candidate lists returned to the LLM are capped at `CANDIDATE_DISPLAY_LIMIT = 300` items per fetch call to manage context window size.*

### 3. The Validation Tool (`validate_targeting`) & Token Optimization
The final step of the LLM's loop MUST be `validate_targeting`. 

- **Token Cost & Latency Optimization (`selected_ids`):** Instead of passing hundreds of heavy JSON candidate objects back into tool arguments (which previously cost 4,000–8,000 output tokens and added 5+ seconds of latency), the LLM passes a lightweight list of string IDs:
  ```json
  {
    "selected_ids": ["6003139266661", "6003123456", "6015678901"]
  }
  ```
- **Context Memory Lookup:** Python looks up each `selected_id` from `_candidate_pool` memory, instantly restoring the complete `TargetingEntity` object (including exact original Meta `type` like `life_events` or `income`).
- **Graph API Validation:** Python batches the entities (by 50) and calls Meta's `/targetingvalidation` endpoint (`GET /act_<id>/targetingvalidation?targeting_list=[...]`).
- **Global Limits:** Applies a hard cap (`TOTAL_TARGETING_LIMIT = 60` segments total across all categories).
- **Session Stash:** Stashes the final validated dictionary into `session_ctx["detailed_targeting"]`.

### 4. Result Assembly & UI Sync
Once the LLM loop finishes, `agent.py` reads the validated dictionary from the session, builds `MetaTargetingSuggestionResult`, and pushes the `targeting_manager` craft block to the UI.

---

## Manual Edits & Direct REST Actions

While `DetailedTargetingAgent` handles full strategic discovery, direct UI chip interactions (typeahead search, adding a segment, deleting a chip) use dedicated REST API endpoints in `targeting_router.py`.

These REST endpoints bypass the LLM entirely, running instant list mutations in **~150ms** with zero LLM token cost:

1. **`GET /sessions/{session_id}/detailed-targeting/search?q={keyword}`**:
   - Queries Meta Graph API directly (`/act_<id>/targetingsearch?q={keyword}`).
   - Stashes candidate metadata in `session_ctx["detailed_targeting_search_results"]`.
   - Returns a compact result list: `[{id, name, type, size}]`.

2. **`POST /sessions/{session_id}/detailed-targeting/segments`**:
   - Adds a segment by ID from prior search results.
   - Resolves full entity metadata from `detailed_targeting_search_results` stashed in the session.
   - Mutates selection and updates session context.

3. **`DELETE /sessions/{session_id}/detailed-targeting/segments/{segment_id}`**:
   - Removes a segment by ID from current detailed targeting selection.
   - Instantly updates session context.

*(Note: Conversational delete requests in chat like "remove the Real Estate segment" use `delete_targeting_segment` in `detailed_targeting_tool.py` via the sub-agent).*

---

## Configuration & Parameters

The Detailed Targeting Agent runs within the Adzump app environment and dynamically resolves its configuration from global settings:

| Component | Controlled By |
|---|---|
| **Model & Tier** | `settings.AGENT_MODEL_TIER` and `settings.ADZUMP_PROVIDER` |
| **Max Tokens** | `settings.AGENT_MAX_TOKENS` |
| **Turns Budget** | `_MAX_TURNS` in `agent.py` (20 turns) |
| **Global Segment Limit** | `TOTAL_TARGETING_LIMIT = 60` in `targeting_tools.py` |
| **Candidate Display Limit** | `CANDIDATE_DISPLAY_LIMIT = 300` per category in `targeting_tools.py` |

**Quirks & Design Decisions:**
- **UI Event Forwarding:** `MetaPassthroughEventStream` forwards user-visible progress, tool executions, text rationale, and craft blocks to the parent stream while ignoring session completion signals.
- **Granular Demographic Type Preservation:** Meta's `/targetingvalidation` endpoint strictly requires exact granular subtypes for demographics (e.g. `life_events`, `income`, `work_positions`). Because `validate_targeting` uses string ID lookups against `_candidate_pool`, Python always sends Meta's exact entity classifier (`e.to_validation_pair()`), guaranteeing 100% validation success for active demographic segments.
- **Direct REST Mutations:** Direct UI chip additions and deletions bypass LLM turns and perform fast state mutations directly against session storage.

