# Geo-Targeting Subsystem

> **Status: implemented 2026-07-03** — code and this doc describe the same system.

## Purpose

Discovers, maps, and edits the geographic areas an ad campaign targets. For a real-estate project in Bandra it finds the nearby localities/pincodes; for a national brand it picks strategic cities/states; it then resolves every area to the **platform-native targeting handle** (Meta adgeolocation `{type, key, name}` or a Google Ads geo-target-constant `resourceName`) that adset creation needs.

The orchestrator (AdzumpAgent) reaches the agent through the `manage_targeting_locations` tool. The widget (map-search click) bypasses the LLM tool entirely and calls the agent's `add`/`delete` methods directly.

---

## How It Works

The `LocationAgent` exposes three public methods: `discover()` (LLM-driven — the LLM picks the path, calls ONE of two tools, and writes a short summary), `add()` and `delete()` (deterministic — no LLM). All three end in the shared `finalize_targets()` (platform-map → persist → re-render). The orchestrator reaches them through the `manage_targeting_locations` tool; the widget reaches `add`/`delete` directly, bypassing the LLM.

### National / International campaign (broad scale)

```
User asks orchestrator for targeting
   │
   ▼
manage_targeting_locations(action="discover")        [tools/location.py]
   │  LLM-facing tool; the orchestrator (AdzumpAgent) calls this
   │
   ▼
LocationAgent.discover(...)                          [agents/location/agent.py]
   │  BaseAgent loop; the LLM runs ONCE
   │
   │  ┌─ LLM reasoning ────────────────────────────────────────┐
   │  │ "National D2C brand in India. Pick 3-6 markets         │
   │  │  (Bengaluru, Mumbai, Delhi, Pune), then call          │
   │  │  geocode_recommendations with them."                  │
   │  └───────────────────────────────────────────────────────┘
   │
   │  ┌─ tool: geocode_recommendations ────────────────────────┐
   │  │  1. Geocode the picked {name, type} via Google Maps    │
   │  │  2. Call platform_mapping.map_target_areas()          │ ← utility
   │  │  3. save_campaign()                                    │ ← utility
   │  │  4. rerender_craft()                                 │ ← utility
   │  │  Returns: list[TargetArea]                            │
   │  └───────────────────────────────────────────────────────┘
   │
   │  ┌─ LLM summary turn ─────────────────────────────────────┐
   │  │ "Targeted 4 cities across India: Bengaluru (tech),    │
   │  │  Mumbai (finance), Delhi NCR (enterprise), Pune         │
   │  │  (emerging). All mapped to Meta + Google handles."     │
   │  └───────────────────────────────────────────────────────┘
   │
   ▼
ToolResult(success=True, data={"target_areas": ..., "summary": "..."})
```

### Local / Real-estate campaign

Same loop, different tool:

```
LocationAgent.discover(...)
   │
   │  LLM reasoning: "Real estate in Bandra. Call discover_neighborhoods."
   │
   │  ┌─ tool: discover_neighborhoods ─────────────────────────┐
   │  │  1. Radial grid scan (~136 points, 8 km default)         │
   │  │  2. Reverse-geocode, dedupe, keep ≤25                 │
   │  │  3. Call platform_mapping.map_target_areas()          │ ← utility
   │  │  4. save_campaign()                                    │ ← utility
   │  │  5. rerender_craft()                                 │ ← utility
   │  │  Returns: list[TargetArea]                            │
   │  └───────────────────────────────────────────────────────┘
   │
   │  LLM summary turn: "Mapped 18 nearby neighborhoods..."
   │
   ▼
ToolResult(success=True, data={"target_areas": ..., "summary": "..."})
```

### Edits (add/delete) — via the orchestrator's tool

```
Orchestrator (AdzumpAgent) decides to add Juhu
   │
   ▼
manage_targeting_locations(action="add", name="Juhu", lat=..., lng=...)
   │
   ▼
LocationAgent.add(...)                                [agents/location/agent.py]
   │  deterministic, no LLM
   │  1. Append area to product.target_areas
   │  2. Call platform_mapping.map_target_areas()     ← utility
   │  3. save_campaign() + rerender_craft()
   │  4. Re-emit suggested_locations SSE
   │
   ▼
ToolResult(success=True, data={"target_areas": ...})
```

Same shape for `delete`. The agent owns the modify logic, but it's deterministic — no `BaseAgent.run()` is invoked.

### Edits (add/delete) — via the widget (no LLM tool, no agent loop)

```
Map-widget click on a search result
   │
   ▼
"add targeting location {\"name\":\"Juhu\",\"lat\":19.1,...}"
   │
   ▼
router → craft.handle_widget_message()                [agents/location/craft.py]
   │  parses + dispatches directly
   │  clears _pending_elicitation (housekeeping)
   │
   ▼
LocationAgent.add(...)                                [agents/location/agent.py]
   │  same method the tool calls — no LLM, no agent loop
   │  just platform_mapping + save + re-render
   │
   ▼
SSE: tool_start, tool_result, done
```

The widget bypasses both the LLM tool (`manage_targeting_locations`) and the agent loop (`BaseAgent.run()`), but the underlying method is the same. One source of truth for the modify flow.

---

## Architecture

```
app/agents/adzump/
├── agents/location/
│   ├── agent.py                LocationAgent (BaseAgent)
│   │                         + get_location_agent() singleton
│   │                         + LocationAgent.discover / .add / .delete
│   ├── context.py              LOCATION_SYSTEM_PROMPT + build_location_context()
│   ├── models.py               All geo types (TargetArea, MetaGeoLocation,
│   │                           GoogleGeoLocation, …)
│   ├── platform_mapping.py     PlatformGeoMapper — area → platform handle
│   │                           (shared utility, called by both tools and by add/delete)
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── discover_neighborhoods.py   ← LLM-callable tool (local path)
│   │   └── geocode_recommendations.py  ← LLM-callable tool (broad path)
│   ├── craft.py                handle_widget_message — router's no-LLM fast path
│   └── AGENT.md                this file
├── services/geo/               ← clean-cut done: only search.py remains
│   └── search.py               autocomplete adapter (used by the map search box UI)
└── tools/
    ├── location.py             LLM-facing tools: confirm_location,
    │                           manage_targeting_locations  ← tool executor dispatches to LocationAgent
    └── craft.py                Craft-panel renderer — NOT the same file as
                                agents/location/craft.py (widget protocol)
```

There are four files whose names collide between layers. Keep them straight:

| File | Scope | What it holds |
|---|---|---|
| `agents/location/agent.py` | the agent | `LocationAgent` (BaseAgent) — exposes `discover`, `add`, `delete` |
| `agents/location/craft.py` | inbound widget protocol | parses + executes map-search clicks with no LLM |
| `agents/location/platform_mapping.py` | shared utility | `PlatformGeoMapper.map_target_areas()` — called by tools and by `add`/`delete` |
| `agents/location/models.py` | all geo types | `TargetArea`, `MetaGeoLocation`, `GoogleGeoLocation` — the resolved shape |
| `agents/location/tools/*.py` | LLM-callable tools | the two tools the agent exposes to the LLM |
| `tools/craft.py` | outbound panel rendering | `emit_craft_panel(...)` writes the SSE craft block |
| `services/geo/search.py` | autocomplete | used by the map search box in the craft panel (UI concern) |

There is no `GeoTargetingService` class. The agent owns all three actions (`discover` / `add` / `delete`); the tool executor is a thin dispatcher.

---

## The Agent (`LocationAgent`)

`LocationAgent` is a `BaseAgent` subclass that exposes three public methods:

- **`discover(params, context)`** — LLM-driven. Runs the BaseAgent loop with two tools; the LLM picks one and writes a summary. Returns a `ToolResult` with the mapped targets and a 1-2 sentence summary.
- **`add(params, context)`** — deterministic. No LLM. Appends the area, calls `platform_mapping.map_target_areas()`, persists, re-renders. Returns a `ToolResult`.
- **`delete(params, context)`** — deterministic. Same shape as `add` but pops the area first.

All three call `platform_mapping.map_target_areas()` as a side effect, so the resolved `TargetArea` is always in the storage layer after a successful action.

### Why an agent, not a direct `provider.create_completion()`

Before this refactor, the LLM call lived in `services/geo/discovery.py :: _discover_strategic_markets` as a bare `provider.create_completion(...)`. That worked but:

- **Token tracking was lost.** The direct call didn't go through `session.record_token_usage(...)`, so the strategist's tokens never hit `ai_tracking_sessions` (FM-06 in the PR #91 review). `BaseAgent.run()` records them on the sub-session.
- **No session, no audit trail.** The call wasn't a sub-session, so the LLM's reasoning (thinking, the tool call, the summary) wasn't persisted anywhere.
- **No structure for multi-step judgment.** A single completion can't reason → act → summarize, retry a failed geocode differently, or grow richer inputs (see Future Improvements). The loop provides that structure — and `max_turns=10` caps the loop the refactor itself introduces (a single completion had no loop to cap; this is the honest accounting).
- **JSON-in-prose parsing.** The strategist returned JSON in free text, guarded by brace-extraction. The tool schema (`geocode_recommendations.locations[{name, type}]`) IS the structured output now — validated by the provider, no parsing.
- **The intelligence was hidden.** A service file carried a bare LLM call while the subsystem advertised itself as fully deterministic. Lifting the call into an agent makes the one model decision visible, owned, and testable.

### Class shape (mirrors `ProductAgent`)

```python
class LocationAgent(BaseAgent):
    display_name = "Location Agent"
    _instance: "LocationAgent | None" = None

    def __init__(self):
        super().__init__(
            name="location_agent",
            tools=LOCATION_AGENT_TOOLS,     # discover_neighborhoods + geocode_recommendations
            context_builder=build_location_context(),
            model_tier="fast",
            max_turns=10,                   # 1 reasoning + 1 tool + 1 summary + slack
            max_tokens=4096,
            provider="anthropic",
        )

    # discover() — LLM-driven, uses BaseAgent.run()
    # add() / delete() — deterministic, no LLM
```

The agent exposes **two tools** to the LLM. The LLM picks one based on the product's `business_scale`. The tools' `execute` functions do the post-processing (platform mapping, persistence, re-render) as a side effect.

### `discover(...)` — the LLM-driven flow

0. **Deterministic preamble.** Resolve + geocode the campaign location (from params / `_location_meta` / `campaign_spec` / product) so the radial-scan tool has coordinates. No LLM.
1. **Sub-session.** `BaseSession(agent_name="location_agent")` — the agent gets its own conversation history and its own token audit row.
2. **Selective context sharing.** Shared *by reference*: `product_data`, `product_profile`, `campaign_spec`, `_location_meta`, `account_names` (+ `competitor_analysis`, craft ids, the parent `_session_id` value) — exactly the keys `finalize_targets`/`save_campaign`/re-render read, so tool writes propagate to the parent. The isolation win is the MESSAGE HISTORY, which stays separate; the shared dicts never enter the sub-LLM's context except through the prompt/tools.
3. **Wrapped event stream.** `_LocationPassthroughEventStream` — drops the agent's `text`/`done`/`error` events, forwards `tool_*` / `agent_*` / `data` / craft events.
4. **Run.** `await self.run(user_message=_build_initial_prompt(product, location_name, country_code), session=sub_session, event_stream=wrapped_stream)` — fast tier from the constructor; the launcher pre-emits `agent_started` and emits `agent_finished` after.
5. **Extract summary + verify.** The final assistant text is the 1-2 sentence summary. Success is judged by the `_geo_finalized` marker `finalize_targets` stamps on the sub-context — a chatty run that never landed targets returns a structured error, not success.

### `add(...)` and `delete(...)` — the deterministic flow

```python
async def add(self, params, context):
    # 1. Mutate product.target_areas (append)
    # 2. platform_mapping.map_target_areas(...)
    # 3. save_campaign(...)
    # 4. _rerender_craft(...)
    # 5. Emit suggested_locations SSE
    return ToolResult(success=True, data={"target_areas": ...})

async def delete(self, params, context):
    # 1. Mutate product.target_areas (pop by 1-based index)
    # 2. platform_mapping.map_target_areas(...)
    # 3. save_campaign(...)
    # 4. _rerender_craft(...)
    return ToolResult(success=True, data={"target_areas": ...})
```

No `BaseAgent.run()`. No LLM call. No token cost. The widget calls these methods directly (bypassing the LLM tool); the orchestrator's `manage_targeting_locations` tool calls them through the dispatcher.

### Sub-session isolation — why it matters

The agent's sub-session is **not** the user's chat session. If we shared the parent's session:

- The agent's internal monologue would appear in the user's chat history.
- The agent's tool calls would show up as "previous turns" the LLM re-reads for context, confusing it.
- Tokens would be mixed in with the parent's token record — no separate audit.

A separate `BaseSession` keeps the agent's reasoning out of the user's view and gives it its own audit trail.

### The prompt

- **System prompt** — `agents/location/context.py :: LOCATION_SYSTEM_PROMPT`. Describes the workflow: "for local/real-estate, call `discover_neighborhoods`; for broad-scale, reason about markets, then call `geocode_recommendations`; exactly one tool call; end with a 1-2 sentence summary; never invent coordinates."
- **User prompt** — `agents/location/agent.py :: _build_initial_prompt(product, location_name, country_code)`. Built per call: business name, category, operating scale, target country, confirmed location, summary (truncated).

### Why two tools, not four

The LLM has only two decisions to make:

1. **Which path** — local (real-estate) or broad (regional/national/international)?
2. **Which markets** (for broad) — the LLM reasons about 3-6 markets based on the business type, country, and scope.

Everything after the tool's call (mapping to Meta/Google, persisting, re-rendering) is mechanical. Making those into LLM-callable tools would add latency, cost, and risk. The post-processing is a side effect of the tool's `execute`, called directly as a Python function — not as a tool the LLM invokes.

### Where the deterministic post-processing actually runs

A common confusion: "does the deterministic part run with the BaseAgent loop?" The answer depends on the action:

- **`discover`** — runs **inside** the agent's loop, as a side effect of the tool's `execute`. `BaseAgent.run()` invokes the tool when the LLM emits a `tool_use` block; the tool's `execute` then calls `platform_mapping.map_target_areas()`, `save_campaign()`, and `rerender_craft()` as Python functions and returns the result. The LLM never sees those as separate tool calls — they're folded into the one tool's `execute`. The loop continues, the LLM writes the summary on a subsequent turn, and `end_turn` exits.
- **`add` / `delete`** — runs **outside** any loop. The agent's method is called directly (by the `manage_targeting_locations` tool executor, or by the widget's `craft.handle_widget_message`). The entire method body is the deterministic work. No `BaseAgent.run()`. No LLM call. No token cost.

In both cases the deterministic work itself is *Python function calls* — `platform_mapping.map_target_areas()`, `save_campaign()`, `rerender_craft()`. The question is only whether those calls happen *during the agent's loop* (as side effects of the tool's `execute` in `discover`) or *outside the loop* (as the body of the `add`/`delete` methods). Neither case treats them as LLM-callable tools.

---

## The Tool Dispatcher (`tools/location.py`)

The `manage_targeting_locations` tool's `execute` function is a thin dispatcher. It does not contain business logic; it routes to the right agent method based on the `action` param:

```python
async def _manage_targeting_locations(params, context):
    action = params.get("action")
    agent = get_location_agent()
    if action == "discover":
        return await agent.discover(params, context)
    elif action == "add":
        return await agent.add(params, context)
    elif action == "delete":
        return await agent.delete(params, context)
    else:
        return ToolResult(success=False, error=f"Invalid action: {action!r}")
```

The orchestrator (AdzumpAgent) calls this tool. The orchestrator's LLM picks the action; the dispatcher routes to the agent; the agent does the work.

---

## The Shared Utility: `platform_mapping.py`

`agents/location/platform_mapping.py` houses `PlatformGeoMapper`, the deterministic area → platform handle resolver. It's **not** a tool — it's a utility called by:

- `tools/discover_neighborhoods.py :: execute` — after the radial scan returns raw targets
- `tools/geocode_recommendations.py :: execute` — after the geocode returns raw targets
- `LocationAgent.add()` and `LocationAgent.delete()` — after the area list changes

The LLM never invokes `platform_mapping` directly. Putting it at the location agent root (not in `tools/`) signals "utility, not LLM-callable."

### What it does

```python
# platform_mapping.py
class PlatformGeoMapper:
    def map_target_areas(
        self,
        target_areas: list[dict],
        platform: str,
        country_code: str,
    ) -> list[dict]:
        """Resolve every area to its Meta/Google handle, returning TargetArea dicts.

        Preserves existing handles on re-map. A failed lookup keeps the area
        (typed, keyless) rather than dropping it. See 'How It Works' for
        the per-platform flow."""
```

---

## The Widget Path (`agents/location/craft.py`)

Machine-readable messages from the craft-panel map — **not natural language**:

```
add targeting location {"name":"Juhu","lat":19.1,"lng":72.83,"pincode":"400049", ...}
delete targeting location index 2
```

Accepted JSON fields: `name, lat, lng, place_id, pincode, city, state, google_id, meta_key, meta_type, index` (plus a `key="value"` fallback format).

`handle_widget_message(agent, session, message)`:
- returns `None` for natural language → the normal agent loop runs;
- otherwise executes directly and streams SSE;
- owns the elicitation housekeeping (clears `_pending_elicitation` so the next real turn doesn't re-ask a chip question);
- calls `LocationAgent.add()` or `LocationAgent.delete()` (no LLM, no agent loop, no tool).

**Invariant, locked by `tests/agents/adzump/agents/location/test_widget_dispatch.py`: this path never calls the LLM.** The test patches `get_llm_provider` to raise if called from the widget path.

---

## Data Model (`agents/location/models.py`)

Every *mapped* location is a `TargetArea` — generic "where" + one nested platform handle:

```jsonc
{ "name": "Pincode 400050", "city": "Mumbai", "state": "MH", "pincode": "400050",
  "lat": 19.06, "lng": 72.83, "distance_km": 1.2, "place_id": "ChIJ…",
  "scale": "city",
  "meta":   { "type": "zip", "key": "IN:400050", "name": "400050" },
  "google": { "resourceName": "geoTargetConstants/1007785", "name": "…" }
}
```

Invariants:
- **`meta.type` is required and non-empty** — Meta adset creation buckets every target by type; a typeless location cannot be constructed.
- `google.resourceName` is normalized to the `geoTargetConstants/{id}` form.
- Field names are **platform-native** (`type`/`key` are Meta's own vocabulary; `resourceName` is Google's).

---

## Entry Points

| # | Path | LLM involved? | Trigger |
|---|---|---|---|
| 1 | `manage_targeting_locations` tool → `LocationAgent.discover/add/delete` | Yes (for `discover` — note: TWO models total, the orchestrator that decided to call + the sub-agent's loop); no (for `add`/`delete`) | Orchestrator (AdzumpAgent) decides; tool executor dispatches |
| 2 | `confirm_location` tool | Elicitation widget (map pin confirm) | Real-estate businesses only |
| 3 | `craft.handle_widget_message` (router fast path) | **Never** — locked by test | Craft-panel map widget add/delete click |
| 4 | Orchestrator EOT auto-mapper | No | Platform set while unmapped `target_areas` exist |
| 5 | `GET /sessions/{id}/target-locations/search` | No | Map search-box autocomplete (`services/geo/search.py`) |

---

## LLM-Facing Tools

### `manage_targeting_locations` (display: "Geo Targeting")

| Param | Type | Notes |
|---|---|---|
| `action` | string, **required**, `enum: discover \| add \| delete` | routes to `LocationAgent.discover/add/delete` |
| `location_name` | string | used by `discover` |
| `index` | integer | 1-based, required for `delete` |
| `name`, `city`, `state`, `pincode` | string | used by `add` |
| `lat`, `lng`, `radius` | number | used by `add` (radius in km) |

**Deliberately NOT exposed to the LLM:** `google_id`, `meta_key`, `meta_type`, `place_id`. Widget-only fields. Exposing them would let the model invent platform IDs with no traceability check.

### `confirm_location` (display: "Confirm Location")

`kind="elicitation"` — emits its own prompt text + map widget atomically. Real-estate only.

---

## External Dependencies

| System | Used for | Via |
|---|---|---|
| Google Maps (geocode/reverse-geocode) | business pin, radial scan, area coords | `adapters/google/maps.py` |
| Google Ads `suggest_geo_targets` | geo-target-constant resolution | `adapters/google/client.py` |
| Meta Marketing `/search` adgeolocation | Meta key/type resolution | `adapters/meta/client.py` |
| AISuggestedData storage | persistence + session-restart hydration | `services/business_storage.py` |
| LLM provider (Anthropic, fast tier) | **The agent only** — reached via `BaseAgent.run()` | `services/llm_provider.py` |

The LLM provider is reached only through `BaseAgent.run()` in the agent. The widget path, the modify methods, the platform mapping, and the autocomplete never call the LLM directly.

---

## Consumers of the Mapped Locations

| Consumer | Reads | Notes |
|---|---|---|
| Orchestrator (`adzump/agent.py`) | `has_mapped_geo_targets` gate, State block, launch payload, summary text | drives `_next_action` |
| Craft panel map (`tools/craft.py`) | `target_areas` + nested `meta`/`google` handles | tooltips show `Meta Key` / `Google resourceName` |
| ds `adzump_session_bridge.py` | per-platform mapped lists | feeds the DS adset builder |

---

## Testing

| File | Covers |
|---|---|
| `tests/agents/adzump/agents/location/test_models.py` | model invariants (required `meta.type`, `resourceName` normalization, platform-only fields) |
| `tests/agents/adzump/agents/location/test_platform_mapping.py` | `PlatformGeoMapper`: Meta/Google handle resolution, scale routing, handle preservation |
| `tests/agents/adzump/agents/location/test_widget_dispatch.py` | widget fast path: NL→None, routes to `LocationAgent.add/.delete`, housekeeping, **no-LLM invariant** |
| `tests/agents/adzump/agents/location/test_strategist_tools.py` | the two tools: coordinates-from-session (never the model), radius, scale-tagging, geocode failure modes → `finalize_targets` |
| `tests/agents/adzump/agents/location/test_agent_add_delete.py` | `LocationAgent.add/delete`: deterministic, no LLM, validation before side effects, mutation → finalize |

Run: `python -m unittest discover -s tests/agents/adzump`.

---

## Design Decisions

- **One agent, three actions.** `discover` (LLM), `add`/`delete` (deterministic). The agent is the single entry point for all location operations.
- **Two tools, not four.** The LLM has only two decisions: which path (local/broad) and which markets. Mapping, persisting, and re-rendering are mechanical and run as side effects of the tool's `execute`.
- **`platform_mapping.py` is a shared utility, not a tool.** Both tools call it; `add` and `delete` call it. The LLM never invokes it directly. Putting it at the location agent root signals "first-class utility, not LLM-callable."
- **No `GeoTargetingService`.** All three actions live on the agent. The tool executor is a thin dispatcher. Minimum service files.
- **Widget fast path never wakes the LLM.** A map click carries every parameter; routing it through the model buys latency + nondeterminism for zero judgment. The widget calls `LocationAgent.add/delete` directly, bypassing the LLM tool AND the agent loop.
- **Sub-session isolation.** The agent's reasoning is not the user's chat. Separate `BaseSession`, separate token record, separate audit trail.
- **`services/geo/` was clean-cut, not stubbed.** All in-repo importers were re-pointed in the same change — including the ORCHESTRATOR (`adzump/agent.py` imported `is_local_business` + `PlatformGeoMapper`) and the craft renderer (`tools/craft.py` imported `is_local_business`). The only file left is `search.py` (map-search autocomplete — a UI concern, not an agent concern). No zombie re-export modules.

---

## Future Improvements

The architecturally-clean split unlocks fixes that were hard to make when the LLM call was a service primitive. Parked for follow-up:

1. **The agent needs better inputs.** The system prompt currently passes `product_name`, `business_type`, `scope`, `country_code`, `summary`. It does NOT see:
   - User-stated target cities (if the user said "we only operate in Bangalore" in chat, the agent doesn't know)
   - Brand's own-cities (scraped from the business website, e.g. footer links, contact page)
   - Tier preference (Tier-1 only, Tier-2 included, etc.)

   For famous brands (Rapido, Zomato) the LLM infers from training data. For non-famous brands, the agent guesses — often wrong. After this refactor, the fix becomes a one-line update to `_build_initial_prompt()`.

2. ~~**Strict structured output.**~~ **Resolved by the design**: the market picks arrive as `geocode_recommendations`' tool-input schema (`locations[{name, type∈{city,state,country}}]`) — provider-validated structure, no JSON-in-prose, no brace-extraction anywhere in the subsystem.

3. **Cache agent runs** per `(product_id, scope, country_code, platform)`. The same product shouldn't re-run the agent on every `discover()` call. TTL-based invalidation is sufficient for the broad-scale case.

4. **A third tool: `user_confirms_cities(...)`.** For ambiguous cases (the LLM is unsure which markets to pick), the tool can elicit the user. Currently the agent picks without confirmation; a tool would let the LLM ask first.

5. **The data-shape defects** (3× duplication, per-platform ghost lists, radius dropped, keyless areas unusable) are documented separately in `TARGETING_SCHEMA_PLAN.html` (repo root). The next planned change is a `geoTargeting` business-level cache + `campaign_spec.targeting` working copy.