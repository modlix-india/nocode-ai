# Geo-Targeting Subsystem

> **Status: implemented 2026-07-03, single-agent re-cut 2026-07-07** - code and this doc describe the same system.

## Purpose

Discovers, maps, and edits the geographic areas an ad campaign targets. For a real-estate project in Bandra it finds the nearby localities/pincodes; for a national brand it picks strategic cities/states; it then resolves every area to the **platform-native targeting handle** (Meta `{type, key, name}` or Google Ads `geoTargetConstants/{id}`) that adset creation needs.

## Architecture

The system follows a **router-specialist** discipline with exactly ONE agent. The orchestrator (Adzump) is a pure router - it does NOT decide which action to take (discover / add / delete) or extract parameters. It only routes "this is a geo-targeting request" to `LocationAgent.handle(user_message)` with the user's verbatim message. `handle()` runs the LocationAgent's own tool-use loop: the prompt carries the business profile + the current targeting list + the verbatim request, and the model acts by picking ONE of four tools. **Intent classification IS tool selection** - there is no separate interpreter LLM and no code-side dispatch.

```
┌─────────────────────────────────────────────────────────────────┐
│  Adzump orchestrator (LLM)                                      │
│  "user wants targeting change" → route                          │
│  call manage_targeting_locations(user_message=<verbatim>)       │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  LocationAgent.handle(user_message, context)                    │
│  guards → geocode business pin → sub-session → self.run(...)    │
│                                                                 │
│  The loop's LLM picks ONE tool (provider = LOCATION_PROVIDER):  │
│    ├─ discover_neighborhoods    (discovery, local scan)         │
│    ├─ geocode_recommendations   (discovery, broad markets)      │
│    ├─ add_location              (deterministic edit)            │
│    └─ delete_location           (deterministic edit)            │
│                                                                 │
│  Every tool ends in finalize_targets (map → persist → render);  │
│  handle() composes the ToolResult from the post-run state.      │
└─────────────────────────────────────────────────────────────────┘
```

**Why this matters:** the orchestrator never reasons over `action` enums, `index` integers, or `lat`/`lng` floats - and neither does any Python dispatch layer. The one LLM that owns geo-targeting sees the *full* state context and expresses its decision as a provider-validated tool call. A previous cut had a second, hidden "intent interpreter" agent in front of the same loop - two agents for one feature, double the prompts/sessions/plumbing for zero capability. One feature, one agent.

### File layout

```
app/agents/adzump/
├── agents/location/
│   ├── agent.py                LocationAgent (BaseAgent)
│   │                           + get_location_agent() singleton
│   │                           + .handle()  (the ONLY orchestrator-facing entry)
│   ├── targeting_run.py        step helpers for one run (validate, geocode,
│   │                           sub-session, prompt, result)
│   ├── subagent_event_stream.py  _LocationPassthroughEventStream
│   ├── context.py              LOCATION_SYSTEM_PROMPT + build_location_context()
│   ├── models.py               TargetArea, MetaGeoLocation, GoogleGeoLocation,
│   │                           AddLocation/DeleteLocation (edit-tool params)
│   ├── platform_mapping.py     PlatformGeoMapper - area → platform handle (utility)
│   ├── search.py               autocomplete (map search-box UI)
│   ├── search_router.py        HTTP route for /target-locations/search
│   │                           (folded into the adzump router - one mount)
│   ├── tools/
│   │   ├── discover_neighborhoods.py   LLM tool (discovery, local path)
│   │   ├── geocode_recommendations.py  LLM tool (discovery, broad path)
│   │   └── edit_locations.py           LLM tools add_location + delete_location
│   └── AGENT.md                this file
└── tools/
    ├── location.py             manage_targeting_locations + confirm_location
    └── craft.py                Craft-panel renderer
```

There is **no widget fast path** - every geo-targeting action (including map clicks) goes through `handle()` via the orchestrator's LLM. There is no `craft.py` under `agents/location/` (was the deleted widget protocol).

---

## Provider configuration

ONE LLM runs on the geo-targeting path - the LocationAgent's own loop:

| Constant | Default | Used by | Set in |
|---|---|---|---|
| `LOCATION_PROVIDER` | `"deepseek"` | the whole `LocationAgent` loop (every action) | `agent.py` |

It passes through the same `LLMProvider` factory (`app.services.llm_provider.get_llm_provider(name)`), so the supported set is: `"anthropic"`, `"openai"`, `"deepseek"`. The loop is tool-use driven, so the provider must support tool-use.

**To switch to Claude** (do this if the fast-tier model mispicks tools or picks weak markets):

```python
# app/agents/adzump/agents/location/agent.py
LOCATION_PROVIDER = "anthropic"
```

Restart the service for the change to take effect (the singleton caches the provider at first instantiation).

**Quirks:**
- Edit params are provider-validated against tool schemas GENERATED from `models.py` (`tool_params_from_model` in `app/core/tools/base.py`), then re-parsed into the pydantic model at the execute boundary - no JSON-in-prose parsing, no hand-rolled envelope check.
- A run that ends without any mutation reaching `finalize_targets` returns `success=False` (the `_geo_finalized` gate), with the model's own final text as the error.
- API key for each provider must be set in env (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`); `LLM_PROVIDER` env var is unrelated to this constant - it controls the global default for agents that don't override.

---

## API endpoint

The agent is reached via the orchestrator's chat endpoint:

```
POST /adzump/sessions/{session_id}/chat
Headers:
    Authorization: Bearer {jwt}
    clientCode: {client_code}
Body:
    {
        "message": "add Juhu to my targeting",   # any natural-language geo request
        "attachments": []                          # optional images (ignored by location agent)
    }
Response:
    SSE stream (text/event-stream)
```

The orchestrator's LLM routes the message to `manage_targeting_locations(user_message=<verbatim>)` when it judges a geo-targeting change is needed. The user does not need to know about the agent - they just type (or click the map) and the orchestrator handles routing.

The **map search box** uses a separate endpoint (UI helper, not the agent):

```
GET /adzump/sessions/{session_id}/target-locations/search?q=Ban&platform=google
```

Returns place-suggestion candidates for the typeahead. Does not call the agent; does not appear in any conversation history.

---

## SSE events

The agent emits the standard agent event types. EVERY action streams the same shape - AgentCard lifecycle around one sub-agent run (only the picked tool differs):

```
event: tool_start
data: {"id": "tc_1", "tool": "manage_targeting_locations",
       "input": {"user_message": "set up geo targeting for Bengaluru"}}

event: agent_started
data: {"agent_id": "location_agent", "label": "Location Agent"}

event: tool_start                       # the loop picks a tool (provider = LOCATION_PROVIDER)
data: {"id": "tc_2", "tool": "geocode_recommendations",
       "input": {"locations": [{"name": "Bengaluru", "type": "city"}, ...]}}

event: tool_result
data: {"id": "tc_2", "tool": "geocode_recommendations",
       "success": true, "summary": "Resolved 4 markets to platform handles"}

event: agent_finished
data: {"agent_id": "location_agent", "status": "success"}

event: tool_result                      # outer tool's ToolResult (audience="both")
data: {"id": "tc_1", "tool": "manage_targeting_locations",
       "success": true,
       "summary": "Targeted 4 cities across India: Bengaluru, Mumbai, ..."}

event: text                             # auto-emitted from audience="both"
data: {"text": "Targeted 4 cities across India: Bengaluru, Mumbai, ..."}

event: done
data: {"session_id": "abc-123", "usage": {...}}
```

For an `add`, `tc_2` is `add_location` with `{"name": "Juhu"}` and the closing text reads like "Added Juhu to targeting - 5 areas total." (`delete` likewise with `delete_location`/`{"index": 2}`). The sub-loop's own `text` events are dropped by the passthrough stream - the model's final summary reaches chat once, via the outer ToolResult's `audience="both"`.

---

## Error handling

The agent returns `ToolResult(success=False, error=<message>)` on failure. The orchestrator surfaces these to the user via the tool card and to its own LLM via the tool_result block.

| Failure | What happens | Recovery path |
|---|---|---|
| Empty `user_message` | `success=False, error="manage_targeting_locations: empty user_message."` (guard fires before the loop) | Orchestrator retries with content; frontend surfaces |
| No auth context | `success=False, error="No auth context available for the location agent."` (guard fires before the loop) | Programmer error |
| Edit tool called with invalid params (e.g. `add_location` without `name`) | The execute's pydantic parse fails → `Invalid params: <field> - <msg>` goes back into the LOOP as the tool result; the model states the failure (no invented retry, per prompt) | The final `ToolResult` is `success=False` with the model's explanation |
| `delete_location` with out-of-range index | Same in-loop path: `"Invalid index N. There are only M target areas."` | Model explains; orchestrator surfaces or asks user |
| Run ends with no mutation reaching `finalize_targets` | `success=False`, error = the model's own final text (or a generic retry hint) - the `_geo_finalized` gate | Orchestrator retries or asks user |
| The loop itself raises (provider down) | Caught in `handle()`; AgentCard closes `status="failed"`; the gate then yields `success=False` | Orchestrator surfaces |

Failed edits are NOT retried by code - the model sees the tool error and explains; only re-invoking `manage_targeting_locations` (a fresh run) retries.

---

## How it works

### Broad campaign (national / international)

```
manage_targeting_locations(user_message="set up geo targeting for Bengaluru")
   │
   ▼
LocationAgent.handle(user_message, context)
   │  Preamble: guards, resolve + geocode business pin (deterministic, no LLM)
   │  Sub-session with shared context refs (product_data, campaign_spec, etc.)
   │  BaseAgent loop runs ONCE on build_run_prompt(profile, list, request)
   │
   │  ┌─ LLM reasoning ──────────────────────────────────────────┐
   │  │ "National D2C brand in India. Pick 3-6 markets and call │
   │  │  geocode_recommendations with them."                    │
   │  └─────────────────────────────────────────────────────────┘
   │
   │  ┌─ tool: geocode_recommendations ──────────────────────────┐
   │  │  1. Geocode the picked {name, type}                       │
   │  │  2. platform_mapping.map_target_areas()                  │
   │  │  3. save_campaign() + rerender_craft()                   │
   │  └─────────────────────────────────────────────────────────┘
   │
   │  ┌─ LLM summary turn ───────────────────────────────────────┐
   │  │ "Targeted 4 cities across India: Bengaluru, Mumbai, ...   │
   │  │  All mapped to Meta + Google handles."                   │
   │  └─────────────────────────────────────────────────────────┘
   │
   ▼
build_run_result → ToolResult(success=True, data={...}, audience="both", ...)
```

### Local / real-estate campaign

Same loop, different tool. The LLM picks `discover_neighborhoods` (radial grid scan ~136 points in 8km default radius, reverse-geocode, dedupe, keep ≤25, then platform-map → save → re-render).

### Edits (add / delete) - same loop, deterministic tools

Map clicks and chat messages go through the same flow:

```
manage_targeting_locations(user_message="add Juhu to targeting")
   │  The orchestrator does NOT pick action="add" or extract name="Juhu"
   │  - it just forwards the user's text.
   ▼
LocationAgent.handle(user_message, context)
   │  Same preamble + loop; the prompt shows the current 1-based list
   │
   │  ┌─ tool: add_location {name: "Juhu"} ──────────────────────┐
   │  │  execute parses params → AddLocation (pydantic boundary) │
   │  │  1. Append area to product.target_areas                  │
   │  │  2. platform_mapping.map_target_areas()                  │
   │  │  3. save_campaign() + rerender_craft()                   │
   │  └─────────────────────────────────────────────────────────┘
   │
   │  LLM summary turn: "Added Juhu - 5 areas total."
   ▼
build_run_result → ToolResult(summary="Added Juhu - 5 areas total.", audience="both", ...)
```

Same shape for `delete` (the model maps "the second one" / "remove Bangalore" to a 1-based `index` using the list in its prompt; the tool pops and re-finalizes).

---

## The Agent (`LocationAgent`)

`LocationAgent` is a `BaseAgent` subclass. Public surface:

- **`handle(user_message, context)`** - the **only** orchestrator-facing entry. Guards, enriches (geocode the business pin), builds a sub-session, then runs the agent's OWN loop once; the model does everything else by picking a tool. Step helpers in `targeting_run.py`.

### Class shape

```python
class LocationAgent(BaseAgent):
    display_name = "Location Agent"

    def __init__(self):
        super().__init__(
            name="location_agent",
            tools=LOCATION_AGENT_TOOLS,     # discover_neighborhoods + geocode_recommendations
                                            # + add_location + delete_location
            context_builder=build_location_context(),
            model_tier="fast",
            max_turns=10,                   # 1 reasoning + 1 tool + 1 summary + slack
            max_tokens=4096,
            provider=LOCATION_PROVIDER,   # see "Provider configuration" - default "deepseek"
            # NOTE: no `context_management` - see "Eviction policy" below.
        )
```

### Why an agent (not a bare `provider.create_completion()`)

The pre-refactor code called `provider.create_completion(...)` directly from a service. That worked but lost:

- **Token tracking** - direct calls didn't go through `session.record_token_usage(...)`; tokens never hit `ai_tracking_sessions`. `BaseAgent.run()` records them.
- **Audit trail** - no sub-session, so the LLM's reasoning wasn't persisted.
- **Multi-step structure** - a single completion can't reason → act → summarize or retry a failed geocode differently. The loop provides that.
- **Structured output** - the strategist returned JSON in free text, guarded by brace-extraction. The tool schema (`geocode_recommendations.locations[{name, type}]`) is now the structured output, provider-validated.

### Eviction policy - intentionally different from `ProductAgent`

`ProductAgent` passes `context_management={"edits": [{"type": "clear_tool_uses_20250919", ...}]}` that auto-clears old tool results past 15k tokens. `LocationAgent` deliberately omits it. The run is bounded: `max_turns=10`, the two tools return at most ~25 neighborhoods or ~6 markets, and the typical shape is 1 reasoning → 1 tool → 1 summary. If this changes (large payloads, higher turn limit), add the same `context_management` block `ProductAgent` uses.

### One run, every action

0. **Preamble** (deterministic, no LLM): guards (empty message, auth), resolve + geocode the business pin so the radial-scan tool has coordinates.
1. **Sub-session** - `BaseSession(agent_name="location_agent")` with shared context refs (`product_data`, `campaign_spec`, `account_names`, etc.). Tools write through to the parent. Message history stays isolated.
2. **Wrapped event stream** - `_LocationPassthroughEventStream` forwards `tool_*` / `craft` / `data` / `agent_*` / `thinking`, drops `text` / `done` / `error`.
3. **Run** - `self.run(build_run_prompt(...), sub_session, wrapped_stream)`; the model picks ONE of the four tools.
4. **Verify + extract** - success is judged by the `_geo_finalized` marker `finalize_targets` stamps on the sub-context. A run where no mutation landed returns a structured error (carrying the model's final text), not success.

---

## The user-facing acknowledgement contract

Every action's user-visible text comes from ONE place: the model writes a 1-2 sentence summary on its final turn. It is captured post-hoc via `BaseAgent._stream_turn` into the sub-session's messages; `build_run_result` re-reads it via `sub_session.get_messages()` and sets `ToolResult.summary = final_text` with `audience="both"`. The orchestrator's framework sees the audience and emits it as chat text.

**Why explicit:** without the `audience` field, the `summary` string lands only in the tool card and in the `tool_result` block sent to the orchestrator's LLM - **never as an SSE text event** - leaving the chat holding only a tool card with no closing sentence (the historic dead-end bug). The sub-loop's own `text` events are dropped by the passthrough stream, so the summary reaches chat exactly once.

**Sibling consumers:** `manage_assets` uses `audience="user"` (`tools/asset_manage.py`); `analyze_competitors` uses `audience="both"` (`tools/competitor.py`).

---

## The tool dispatcher (`tools/location.py`)

The `manage_targeting_locations` tool's `execute` is a thin forwarder to `LocationAgent.handle()`:

```python
async def _manage_targeting_locations(params, context):
    user_message = (params.get("user_message") or "").strip()
    if not user_message:
        return ToolResult(success=False, error="manage_targeting_locations requires a user_message")
    return await get_location_agent().handle(user_message, context)
```

The orchestrator's LLM picks this tool when the user wants a targeting change; it forwards the user's verbatim message; `handle()` does the interpretation + dispatch.

`confirm_location` is a separate tool: real-estate-only elicitation that emits a map widget + prompt atomically. Does not route through `LocationAgent`.

---

## The shared utility: `platform_mapping.py`

`PlatformGeoMapper` resolves areas to platform-native handles. Called as a Python function (not an LLM tool) from `tools/_shared.finalize_targets` - the funnel all four tools end in:

- `tools/discover_neighborhoods.py :: execute`
- `tools/geocode_recommendations.py :: execute`
- `tools/edit_locations.py :: add_location / delete_location executes`

Putting it at the location agent root (not in `tools/`) signals "utility, not LLM-callable."

---

## Data model

Every mapped location is a `TargetArea` - generic "where" + nested platform handles:

```jsonc
{ "name": "Pincode 400050", "city": "Mumbai", "state": "MH", "pincode": "400050",
  "lat": 19.06, "lng": 72.83, "distance_km": 1.2, "place_id": "ChIJ…",
  "scale": "city",
  "meta":   { "type": "zip", "key": "IN:400050", "name": "400050" },
  "google": { "resourceName": "geoTargetConstants/1007785", "name": "…" }
}
```

Invariants:
- **`meta.type` and `meta.key` are required and non-empty** - Meta rejects an entry lacking either; a failed key lookup attaches NO handle.
- **`google.resourceName` is required**, normalized to `geoTargetConstants/{id}` form; no constant → no handle.
- Field names are platform-native (`type`/`key` = Meta, `resourceName` = Google).

---

## Entry points

| Path | LLM involved? | Trigger |
|---|---|---|
| `manage_targeting_locations(user_message=...)` → `LocationAgent.handle` → the loop picks one of 4 tools | Yes - orchestrator routes, the LocationAgent's own loop acts | Orchestrator routes a geo-targeting message |
| `confirm_location` tool | No | Real-estate only - map pin confirmation |
| Orchestrator EOT auto-mapper | No | Platform set while unmapped `target_areas` exist |
| `GET /sessions/{id}/target-locations/search` | No | Map search-box autocomplete (UI helper, separate router) |

---

## LLM-facing tools

### `manage_targeting_locations` (display: "Geo Targeting")

| Param | Type | Notes |
|---|---|---|
| `user_message` | string, **required** | The user's verbatim message. Examples: `"set targeting for Bangalore"`, `"add Mumbai at 19.07 72.87"`, `"delete the second area"`. The subsystem interprets intent + extracts params - the orchestrator does NOT pre-classify. |

**Deliberately NOT exposed:** `action`, `index`, `name`, `lat`, `lng`, `radius`, `key`, `type`, `resourceName`, `place_id`. All of these belong to the LocationAgent's own tool picks (or are widget-only fields). Exposing them would let the orchestrator LLM fabricate structured parameters with no traceability check.

### `confirm_location` (display: "Confirm Location")

`kind="elicitation"` - emits its own prompt text + map widget atomically. Real-estate only.

---

## External dependencies

| System | Used for | Via |
|---|---|---|
| Google Maps (geocode/reverse-geocode) | business pin, radial scan, area coords | `adapters/google/maps.py` |
| Google Ads `suggest_geo_targets` | geo-target-constant resolution | `adapters/google/client.py` |
| Meta Marketing `/search` adgeolocation | Meta key/type resolution | `adapters/meta/client.py` |
| AISuggestedData | persistence + session-restart hydration | `services/business_storage.py` |
| LLM provider - default `LOCATION_PROVIDER="deepseek"` | the whole LocationAgent loop | `services/llm_provider.py` |
| LLM provider (Anthropic / OpenAI) | optional switch via the constant | `services/llm_provider.py` |

The LLM is reached only through `BaseAgent.run()` inside `handle()`. The tool executes, platform mapping, and autocomplete never call the LLM directly. See [Provider configuration](#provider-configuration) for how to switch providers.

---

## Testing

| File | Covers |
|---|---|
| `tests/agents/adzump/agents/location/test_models.py` | model invariants (`meta.type` required, `resourceName` normalization) |
| `tests/agents/adzump/agents/location/test_platform_mapping.py` | `PlatformGeoMapper`: Meta/Google handle resolution, scale routing, handle preservation |
| `tests/agents/adzump/agents/location/test_agent.py` | handle: empty-message/auth guards, prompt carries the verbatim request + 1-based list, `_geo_finalized` result gating, run-exception → structured error, the four-tool registry |
| `tests/agents/adzump/agents/location/test_strategist_tools.py` | the two discovery tools: coordinates-from-session, radius, scale-tagging, geocode failure modes |
| `tests/agents/adzump/agents/location/test_edit_locations.py` | `add_location`/`delete_location` executes: pydantic param boundary, mutation → finalize, out-of-range index, schemas mirror the params models |
| `tests/agents/adzump/agents/location/test_targeting_run.py` | run-prompt rendering (verbatim request, 1-based list, summary truncation) |

Run: `python -m unittest discover -s tests/agents/adzump`.

---

## Design decisions

- **One agent, one loop, four tools.** The LocationAgent's loop IS the intent interpreter - discovery and manual edits are just different tools it can pick. A previous cut ran a second hidden "interpreter" agent in front of the loop and a Python dispatch behind it; both are gone (two agents for one feature is unmanageable - sub-agents are only for genuinely DIFFERENT features where one agent doing both would hallucinate).
- **Tools carry no LLM judgment.** Mapping, persisting, re-rendering are mechanical and run as side effects of each tool's `execute`; edit params are re-parsed into pydantic models at the execute boundary.
- **`platform_mapping.py` is a utility, not a tool.** Both tools call it; `add`/`delete` call it. The LLM never invokes it directly.
- **No `GeoTargetingService`.** All three actions live on the agent. Minimum service files.
- **Sub-session isolation.** The agent's reasoning is not the user's chat. Separate `BaseSession`, separate token record, separate audit trail.
- **User-facing acknowledgement uses `audience="user"`, not a prompt-only rule.** Prompt-only rules were tried (commit `87cc5a4`, "capture-ack steer") and broke under model drift. The `audience=` mechanism is the deterministic fix.
- **`services/geo/` was dissolved, not stubbed.** All in-repo importers re-pointed in the same change; its survivors (`search.py` + the UI-helper route, now `search_router.py`) moved into this package - the location agent owns geo search - and the route is folded into the adzump router so main.py mounts one router.

---

## Future improvements

1. **The agent needs better inputs.** The system prompt currently passes `product_name`, `business_type`, `scope`, `country_code`, `summary`. It does NOT see:
   - User-stated target cities (if the user said "we only operate in Bangalore" in chat)
   - Brand's own-cities (scraped from the business website)
   - Tier preference (Tier-1 only, Tier-2 included, etc.)

   For famous brands (Rapido, Zomato) the LLM infers from training data. For non-famous brands, the agent guesses - often wrong. After the refactor, the fix is a one-line update to `build_run_prompt()`.

2. ~~**Strict structured output.**~~ **Resolved by the design**: market picks arrive as `geocode_recommendations.locations[{name, type∈{city,state,country}}]` - provider-validated structure, no JSON-in-prose parsing.

3. **Cache discovery runs** per `(product_id, scope, country_code, platform)`. The same product shouldn't re-run discovery every time. TTL-based invalidation is sufficient for the broad-scale case.

4. **A fifth tool: `user_confirms_cities(...)`.** For ambiguous cases (the LLM is unsure which markets to pick), the tool can elicit the user. Currently the agent picks without confirmation.

5. **The data-shape defects** (3× duplication, per-platform ghost lists, radius dropped, keyless areas unusable) are documented separately in `TARGETING_SCHEMA_PLAN.html`. The next planned change is a `geoTargeting` business-level cache + `campaign_spec.targeting` working copy.

6. **FM-09 - stale mapped-location IDs across sessions.** *(plain-language walkthrough)*

   **The setup.** When a user picks a target city - say "Bengaluru" - our system looks up the *platform's internal ID* for it on Meta and Google, because those platforms don't accept city names, they accept their own numeric IDs:
   - On Meta, "Bengaluru" maps to something like `meta.key = "23424848"` with `meta.type = "city"`.
   - On Google Ads, "Bengaluru" maps to `google.resourceName = "geoTargetConstants/1026181"`.

   Those IDs ride nested on each `product_data.target_areas` entry (`area.meta` / `area.google`) and are projected into the stored record as `campaign.googleMappedLocations` / `metaMappedLocations`. On the next session, the orchestrator's gate `CampaignContext.has_mapped_geo_targets` (`next_action.py`) checks: *"do we have these IDs cached?"* - if yes, the orchestrator skips re-mapping and reuses the cached IDs as-is.

   **The risk.** Meta and Google periodically reorganise their geo catalogs. They merge cities, split districts, drop legacy IDs, renumber regions. When they do:
   - An ID that used to mean "Bengaluru" might now point to "Mysuru" (silent mis-targeting).
   - Or the ID might be deleted entirely (publish fails with a cryptic 404).
   - **Either way, our gate still passes**, because `has_mapped_geo_targets` only checks for *presence* of the ID, not its *currency*. The orchestrator happily treats stale data as fresh.

   **Why it bites in practice.** A real-estate client in Bangalore builds a campaign today. They come back a quarter later to tweak the budget. Our orchestrator sees `google_mapped_locations: [...]` and says "geo targeting is set, all good." The client launches - but Meta's `meta.key = "23424848"` was reassigned six weeks ago. Their ads now run in Mysuru. They don't find out until they get suspicious leads from a city they never targeted.

   **What "fix" looks like.** Two complementary options:
   - **TTL on the mapping** - store a `mapped_at` timestamp alongside each ID; if `now - mapped_at > N days` (e.g. 30), treat as un-mapped and re-look-up before the orchestrator reuses it. Cheap, but blunt (might re-map IDs that are still valid).
   - **Re-map trigger before reuse** - before reusing cached IDs, ping Meta/Google and ask "does this ID still exist and resolve to the original name?" If yes, refresh `mapped_at`. If no, drop and re-run discovery via `manage_targeting_locations`. More precise; extra API call only for stale-risk campaigns.

   **Status today.** Not fixed. Documented here so the next person who touches the geo-mapping layer sees it before they ship.