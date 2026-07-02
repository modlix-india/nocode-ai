# Geo-Targeting Subsystem (GeoTargetingService)

## Purpose

Discovers, maps, and edits the geographic areas an ad campaign targets. For a
real-estate project in Bandra it finds the nearby localities/pincodes; for a
national brand it picks strategic cities/states; it then resolves every area to
the **platform-native targeting handle** (Meta adgeolocation `{type, key}` or a
Google Ads geo-target-constant `resourceName`) that adset creation needs.

**This is deliberately a SERVICE, not an agent.** In this codebase "agent"
means an LLM loop with its own tools (cf. the Product Analyst under
`agents/product/`). The geo pipeline is lookups, geometry, and IO — putting a
model in that loop would add latency and nondeterminism for zero judgment. The
guiding split: *the prompt is the brain, the harness is the reliability.*

Exactly **one model decision** exists in the subsystem:

| Decision | Where | Prompt |
|---|---|---|
| Broad-scale market picking ("which cities/states for a national campaign?") | `services/geo/discovery.py :: _discover_strategic_markets` (single structured-output call, `model_tier="fast"`) | `services/geo/prompts.py` |

Everything else — radial neighborhood scanning, platform-key resolution,
persistence, widget handling — is deterministic Python. Interpreting the
user's *language* about locations ("add Andheri", "remove the second one") is
the **orchestrator** LLM's job; it calls this service through tools.

---

## How It Works

```
User picks platform → orchestrator's <system-reminder> prescribes:
  manage_targeting_locations(action="discover")
  │
  ▼
GeoTargetingService.discover                        (agents/location/agent.py)
  ├─ geocode the confirmed business location        (google_maps_client)
  ├─ discover raw areas                             (services/geo/discovery.py)
  │    ├─ local / real-estate → radial grid scan    49-point reverse-geocode grid,
  │    │                                            8 km default, ≤25 neighborhoods
  │    └─ regional/national/international
  │         → strategist LLM (THE one model call)   3–6 cities/states/countries + scale
  ├─ map each area to the platform                  (services/geo/mapping.py)
  │    ├─ Meta:   /search type=adgeolocation        → meta: {type, key, name}
  │    └─ Google: suggest_geo_targets               → google: {resourceName, name}
  ├─ persist                                        save_campaign → AISuggestedData
  ├─ re-render the craft panel map                  (tools/craft.py emit)
  └─ emit `suggested_locations` SSE data event      (location chips in the UI)
```

Edits then arrive on **two paths** — one intelligent, one deterministic:

```
"also target Juhu"           map-widget click on a search result
        │                             │
        ▼                             ▼
orchestrator LLM interprets   craft-panel sends machine-readable message:
        │                     'add targeting location {"name":"Juhu","lat":…}'
        ▼                             │
manage_targeting_locations    router → handle_widget_message()   ← NO LLM, ever
(action="add", name="Juhu")           (agents/location/craft.py)
        └────────────┬────────────────┘
                     ▼
        GeoTargetingService.modify → re-map → persist → re-render
```

---

## Architecture

```
app/agents/adzump/
├── agents/location/
│   ├── agent.py         GeoTargetingService — discover/modify orchestration
│   │                    (singleton; NO LLM call in this class)
│   ├── craft.py         Widget protocol: parse_location_widget_message +
│   │                    handle_widget_message (router's no-LLM fast path,
│   │                    owns parsing, elicitation housekeeping, dispatch, SSE)
│   └── AGENT.md         this file
├── services/geo/
│   ├── discovery.py     Radial grid scan (local) + strategist LLM (broad)
│   ├── mapping.py       PlatformGeoMapper — area → platform handle
│   ├── models.py        TargetArea, MetaGeoLocation, GoogleGeoLocation
│   ├── prompts.py       Strategist system + user prompt (the subsystem's brain)
│   └── search.py        Autocomplete for the map search box
└── tools/
    ├── location.py      LLM-facing tools: confirm_location,
    │                    manage_targeting_locations (thin dispatch to the service)
    └── craft.py         Craft-panel renderer (map block payload) — NOT the same
                         file as agents/location/craft.py (widget protocol)
```

Note the two `craft.py` files: `agents/location/craft.py` = inbound widget protocol;
`tools/craft.py` = outbound panel rendering.

---

## Entry Points

| # | Path | LLM involved? | Trigger |
|---|---|---|---|
| 1 | `manage_targeting_locations` tool → service | Orchestrator decides to call; service is deterministic | `_next_action` prescription or user request in chat |
| 2 | `confirm_location` tool | Elicitation widget (map pin confirm) | Real-estate businesses only (`is_real_estate` gate) |
| 3 | `handle_widget_message` (router fast path) | **Never** — locked by test | Craft-panel map widget add/delete click |
| 4 | Orchestrator EOT auto-mapper (`adzump/agent.py`) | No | Platform set while unmapped `target_areas` exist |
| 5 | `GET /sessions/{id}/target-locations/search` | No | Map search-box autocomplete (`services/geo/search.py`) |

---

## LLM-Facing Tools

### `manage_targeting_locations` (display: "Geo Targeting")

| Param | Type | Notes |
|---|---|---|
| `action` | string, **required**, `enum: discover \| add \| delete` | routes inside the service |
| `location_name` | string | used by `discover` |
| `index` | integer | 1-based, required for `delete` |
| `name`, `city`, `state`, `pincode` | string | used by `add` |
| `lat`, `lng`, `radius` | number | used by `add` (radius in km) |

**Deliberately NOT exposed to the LLM:** `google_id`, `meta_key`, `meta_type`,
`place_id`. These are the widget wire format's fields — the widget path calls
the service directly and bypasses this schema. Exposing them would let the
model invent platform IDs with no traceability check (accounts are
fetch-traceable; geo keys would not be).

### `confirm_location` (display: "Confirm Location")

`kind="elicitation"` — emits its own prompt text + map widget atomically
(the model must not restate the question). Real-estate businesses only.

---

## Widget Protocol (`agents/location/craft.py`)

Machine-readable messages from the craft-panel map — **not natural language**:

```
add targeting location {"name":"Juhu","lat":19.1,"lng":72.83,"pincode":"400049", ...}
delete targeting location index 2
```

Accepted JSON fields: `name, lat, lng, place_id, pincode, city, state,
google_id, meta_key, meta_type, index` (plus a `key="value"` fallback format).

`handle_widget_message(agent, session, message)`:
- returns `None` for natural language → the normal agent loop runs;
- otherwise executes directly and streams SSE:

```
event: tool_start   {"id": "widget_location", "tool": "manage_targeting_locations", ...}
event: tool_result  {"id": "widget_location", "success": true, "summary": "..."}
event: done         {"session_id": "..."}
```

It also owns the elicitation housekeeping (clears `_pending_elicitation` so
the next real turn doesn't re-ask a chip question). **Invariant, locked by
`tests/agents/adzump/agents/location/test_widget_dispatch.py`: this path never
calls the LLM.**

---

## Data Model (`services/geo/models.py`)

Every mapped location is a `TargetArea` — generic "where" + one nested,
platform-native handle:

```jsonc
{ "name": "Pincode 400050", "city": "Mumbai", "state": "MH", "pincode": "400050",
  "lat": 19.06, "lng": 72.83, "distance_km": 1.2, "place_id": "ChIJ…",
  "scale": "city",                                  // strategist output only (broad campaigns)
  "meta":   { "type": "zip", "key": "IN:400050", "name": "400050" },   // Meta campaigns
  "google": { "resourceName": "geoTargetConstants/1007785", "name": "…" } // Google campaigns
}
```

Invariants enforced by the models:
- **`meta.type` is required and non-empty** — Meta adset creation buckets every
  target by type (`zips`/`cities`/`regions`/`countries`); a typeless location
  cannot be constructed (this was the original production bug).
- `google.resourceName` is normalized to the `geoTargetConstants/{id}` form.
- Field names are **platform-native** (`key`/`type` are Meta's own vocabulary;
  `resourceName` is Google's) — consumers never translate.

Session/storage placement (current):

```
session.context._location_meta.{meta,google}_mapped_locations   ← per-platform lists
session.context.product_data.target_areas (+ same per-platform keys)
record.campaign.{targetAreas, metaMappedLocations, googleMappedLocations}
```

⚠ This shape has known defects (3× duplication, per-platform ghost lists on a
platform switch, radius dropped, keyless areas unusable). The redesign — a
business-level `geoTargeting` cache + a `campaign_spec.targeting` working
copy — is specified in `TARGETING_SCHEMA_PLAN.html` (repo root) and is the
next planned change to this subsystem. Read that before touching the schema.

---

## Discovery Internals (`services/geo/discovery.py`)

| Scale | Method | Details |
|---|---|---|
| `local` (incl. real estate) | Radial grid scan | concentric rings at 0.33/0.66/1.0 × radius (default 8 km), reverse-geocode ~49 points, dedupe by pincode/neighborhood, keep ≤25 sorted by distance |
| `regional` / `national` / `international` | Strategist LLM | 3–6 locations, each tagged `type: city\|state\|country` (carried as `scale` for platform mapping); prompt in `services/geo/prompts.py`; JSON parse is fence/prose-tolerant (brace extraction) |

Platform mapping (`services/geo/mapping.py`) then resolves handles per area:
Meta `/search?type=adgeolocation&location_types=[…]` (pincode→`zip`,
`scale`→`country`/`region`, else `city`; prefers Meta's canonical `type` from
the response); Google `suggest_geo_targets`. Lookup misses keep the area
(typed, keyless) rather than dropping it. Existing handles are preserved on
re-map — a failed lookup never erases a resolved key.

---

## External Dependencies

| System | Used for | Via |
|---|---|---|
| Google Maps (geocode/reverse-geocode) | business pin, radial scan, area coords | `adapters/google/maps.py` |
| Google Ads `suggest_geo_targets` | geo-target-constant resolution | `adapters/google/client.py` (ds gateway, user auth headers) |
| Meta Marketing `/search` adgeolocation | Meta key/type resolution | `adapters/meta/client.py` |
| AISuggestedData storage | persistence + session-restart hydration | `services/business_storage.py` |
| LLM provider (`fast` tier) | strategist only | `services/llm_provider.py` |

---

## Consumers of the Mapped Locations

| Consumer | Reads | Notes |
|---|---|---|
| Orchestrator (`adzump/agent.py`) | `has_mapped_geo_targets` gate, State block, launch payload | drives `_next_action` |
| Craft panel map (`tools/craft.py` → nocode-ui `CraftRenderer.tsx`) | `target_areas` + nested `meta`/`google` handles | tooltips show `Meta Key` / `Google resourceName` |
| ds `adzump_session_bridge.py` | per-platform mapped lists | feeds the DS adset builder (`Location{key,name,type}` — the nested `meta` handle maps 1:1) |

---

## Testing

| File | Covers |
|---|---|
| `tests/agents/adzump/services/test_geo_models.py` | model invariants (required `meta.type`, `resourceName` normalization, platform-only fields) |
| `tests/agents/adzump/services/test_geo_mapping_meta.py` | mapper behavior: type on success/miss/exception, canonical-type preference, scale routing, handle preservation, Google resource names |
| `tests/agents/adzump/agents/location/test_widget_dispatch.py` | widget fast path: NL→None, add/delete dispatch, housekeeping, **no-LLM invariant** |

Run: `python -m unittest discover -s tests/agents/adzump` (pytest not installed — use unittest).

---

## Design Decisions (why it is the way it is)

- **Service, not sub-agent.** The pipeline contains no judgment; a
  persona+tools loop is deferred until a real semantic-editing requirement
  ("drop anything >10 km") exists and is eval-proven. Until then the
  orchestrator interprets language, the service executes.
- **Widget fast path never wakes the LLM.** A map click carries every
  parameter; routing it through the model buys latency + nondeterminism for
  zero judgment. The geo layer (not the HTTP router) owns the whole protocol.
- **One prompt, first-class.** The strategist prompt lives in
  `services/geo/prompts.py`, not inline — reviewable and enrichable without
  touching pipeline code.
- **Platform handles are nested and platform-native** so no consumer ever
  guesses field semantics (`meta.type`/`meta.key`, `google.resourceName`).

For the full schema-migration plan, review history, and the open
bugs/limits, see `TARGETING_SCHEMA_PLAN.html` and `PR91_REVIEW.html` in the
repo root.
