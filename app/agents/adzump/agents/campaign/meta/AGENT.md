# Meta Detailed Targeting Subsystem

> **Status: active** - code and this doc describe the same system.

## Purpose

Discovers, maps, filters, and validates detailed targeting segments (Interests, Behaviors, and Demographics) for Meta Ads campaigns. For a B2B SaaS product, it might find specific software platforms and job titles; for a local gym, it finds fitness interests and life events. It heavily curates options from the Meta Graph API and strictly returns valid, active Meta segment IDs that are natively required for Ad Set creation.

## Architecture

The system follows a **sub-agent orchestration** discipline. The main Adzump orchestrator does not perform granular segment searches. Instead, it delegates to the `DetailedTargetingAgent` via the `suggest_meta_targeting` tool. 

The `DetailedTargetingAgent` spins up its own focused LLM tool-use loop. The prompt carries the overarching business profile, the campaign specs, and specific strict rules. The model then acts by orchestrating a sequence of API calls using four specific tools.

```text
┌─────────────────────────────────────────────────────────────────┐
│  Adzump orchestrator (LLM)                                      │
│  "User wants detailed targeting strategy" → route               │
│  call suggest_meta_targeting(ad_account_id=..., user_query=...) │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  DetailedTargetingAgent.recommend()                             │
│  Sets up isolated sub-session → self.run(...)                   │
│                                                                 │
│  The loop's LLM executes a multi-step sequence using 4 tools:   │
│    ├─ fetch_interests    (search + suggestions expansion)       │
│    ├─ fetch_behaviors    (catalog browse + search)              │
│    ├─ fetch_demographics (taxonomy browse + 8-subtype search)   │
│    └─ validate_targeting (FINAL: API validity check & limit)    │
│                                                                 │
│  Once complete, state is stashed back into parent context and   │
│  rendered to the UI via the `targeting_manager` craft block.    │
└─────────────────────────────────────────────────────────────────┘
```

**Why this matters:** The main orchestrator doesn't get bloated by handling Meta Graph API specifics, deduplication, or audience size limits. The sub-agent isolates the complexity. The LLM is forced to define a `<Strategy>` block *first*, then aggressively fetches diverse candidate segments, and must end by calling the strict `validate_targeting` tool which discards inactive IDs via Meta's `targetingvalidation` endpoint.

### File Layout

```text
app/agents/adzump/
├── adapters/meta/
│   └── targeting_adapter.py    Low-level Meta Graph API HTTP calls (search, browse, validate)
├── agents/campaign/meta/
│   ├── agent.py                DetailedTargetingAgent (BaseAgent)
│   │                           + .recommend() (entry point for the sub-session)
│   ├── context.py              System prompt & rules for DetailedTargetingAgent
│   ├── models.py               Pydantic models (TargetingEntity, MetaTargetingSuggestionResult)
│   ├── subagent_event_stream.py MetaPassthroughEventStream (UI noise filtering)
│   ├── AGENT.md                This file
│   └── tools/
│       ├── targeting_tools.py  The 4 inner loop LLM tools (fetch_*, validate_targeting)
│       └── detailed_targeting_tool.py The outer orchestrator tools (suggest_meta_targeting, modify_meta_targeting)
```

## The Sub-Agent Tool Loop (Discovery & Validation)

When the orchestrator delegates to the sub-agent (via `suggest_meta_targeting`), the `DetailedTargetingAgent` takes over in an isolated loop with access to 4 specific tools.

### 1. The Strategy Requirement
Before calling any tools, the LLM is explicitly instructed by `context.py` to output a short `<Strategy>` block defining the buyer persona and search rationale. The `MetaPassthroughEventStream` intercepts this text and forwards it directly to the UI's chat window, keeping the user informed of the AI's logic before the heavy API calls begin.

### 2. The Fetch Tools
The LLM selects diverse seeds (e.g. brand names, job titles) and passes them to the fetch tools. Each tool uses a different Graph API strategy optimized for that category:
- **`fetch_interests`**: Uses a two-phase approach. It first searches for the seed keywords, then takes the resulting IDs and runs a batched `targetingsuggestions` expansion query to find hidden/related interests.
- **`fetch_behaviors`**: Combines a full `targetingbrowse` of the behavior catalog with parallel keyword searches.
- **`fetch_demographics`**: Combines a full taxonomy browse with parallel searches across all 8 specific demographic subtypes (e.g., `life_events`, `income`, `education_majors`).

*Note: Before returning candidates to the LLM, the lists are sorted descending by `audience_size_upper_bound` and capped at 100 items to guarantee maximum reach and save token context.*

### 3. The Validation Tool
The final step of the LLM's loop MUST be `validate_targeting`. 
- The LLM passes its curated, filtered segments to this tool.
- The tool batches the IDs (by 50) and hits Meta's `/targetingvalidation` endpoint to aggressively discard any deprecated or inactive segments.
- It applies hard category caps (Interests: 25, Behaviors: 20, Demographics: 15).
- It stashes the final validated dictionary directly into `session_ctx["detailed_targeting"]`.

### 4. Result Assembly & UI Sync
Once the LLM loop finishes, `agent.py` pulls the stashed targeting dict from the session, builds the final structured `MetaTargetingSuggestionResult`, and pushes the `targeting_manager` craft block to the UI.

---

## Manual Edits & Direct Actions

While the `DetailedTargetingAgent` sub-agent handles full strategy discovery, there is a secondary orchestrator-level tool (`modify_meta_targeting` in `detailed_targeting_tool.py`) used for surgical, single-action edits. 

This tool is invoked when a human user interacts with the frontend UI or explicitly asks the LLM to search for a specific keyword. It bypasses the sub-agent and directly mutates the session state.

The tool accepts three `action` modes:

1. **`search`**: 
   - Queries the Meta Graph API directly (`/targetingsearch`) for a specific keyword.
   - Saves the rich metadata (exact types, audience sizes, category paths) temporarily into `session_ctx["detailed_targeting_search_results"]`.
   - Renders the search dropdown in the UI.

2. **`add`**: 
   - Attempts to find the `target_id` in the `detailed_targeting_search_results`.
   - **If found:** Builds a complete `TargetingEntity` using the rich metadata fetched during the search phase.
   - **Fallback (If not found):** If the LLM hallucinates an ID, or the search state was cleared, it creates a fallback entity. It safely sets the `type` and `category` to the generic top-level bucket (e.g., `"demographics"` or `"interests"`) to prevent incorrect granular mapping (e.g., mislabeling an education major as a work position).
   - Appends it to the category list and clears the temporary search results.

3. **`delete`**: 
   - Scans all three category buckets (`interests`, `behaviors`, `demographics`) for the matching `target_id`.
   - Filters it out of the list and saves the updated state back to the session.

Every manual action ends by pushing the updated targeting state directly to the UI via the `targeting_manager` craft block, ensuring the frontend stays perfectly synced with the backend session memory.

---

## Configuration & Parameters

The Detailed Targeting Agent runs within the Adzump app environment and dynamically resolves its configuration from global settings, unlike older hardcoded agents.

| Component | Controlled By |
|---|---|
| **Model & Tier** | `settings.AGENT_MODEL_TIER` and `settings.ADZUMP_PROVIDER` |
| **Max Tokens** | `settings.AGENT_MAX_TOKENS` |
| **Turns Budget** | `_MAX_TURNS` in `agent.py` |

**Quirks & Design Decisions:**
- **UI Event Filtering:** The sub-agent loop can generate a massive amount of internal text reasoning and tool calls. To prevent UI flickering and confusing the user, the `MetaPassthroughEventStream` intentionally forwards only specific text messages and the final craft result, shielding the frontend from the intermediate tool noise.
- **Demographic Validation Exception:** Meta's `/targetingvalidation` endpoint strictly requires exact granular subtypes for demographics. To prevent complex API errors, `validate_targeting` explicitly skips demographics and assumes whatever the LLM selected from the initial search is valid.
- **Sorting by Audience Size:** Before the candidate pool is handed to the LLM (which is capped to 100 per category to save context tokens), the segments are sorted descending by `audience_size_upper_bound` to ensure only the highest-reach audiences are evaluated.
