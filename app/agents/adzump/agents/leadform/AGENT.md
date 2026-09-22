# Lead Form Subsystem

## Purpose

Creates, edits, previews, and publishes Meta Instant Forms for Lead Generation campaigns. It acts as a dedicated sub-agent that understands Meta's strict Instant Form schema (character limits, question types, privacy policies) and allows the user to conversationally edit the draft form before it is physically created on Facebook. 

It optionally analyzes the advertiser's historical lead forms (if they have run campaigns before) to detect reusable patterns, but always prioritizes the current `BusinessContext` to prevent hallucinating stale prices or incorrect URLs.

## Architecture

The system follows a **router-specialist** discipline, structured as a Phase Machine. The main orchestrator routes to the `suggest_lead_form` tool (in `parent_tool.py`), which acts as a thin wrapper that invokes `run_leadform_session` (in `agent.py`). 

The `LeadFormAgent` runs its own localized loop, operating in two distinct modes:

1. **GENERATE Mode:** Cold-start generation. Moves through `STRATEGY` → `ANALYZE` (if historical forms exist) → `RECOMMEND` phases to build a draft form.
2. **MANAGE Mode:** Conversational editing. The agent loads the existing draft from the database, processes the user's explicit requested changes, and uses the `update_form_recommendation` tool to mutate the draft and re-render the UI.

```
┌─────────────────────────────────────────────────────────────────┐
│  Adzump Orchestrator (LLM)                                      │
│  "User wants to create/edit a lead form" → route                │
│  call suggest_lead_form(user_message=<verbatim>)                │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  run_leadform_session(agent.py)                                 │
│  1. Merges parent context into localized sub-session            │
│  2. Resolves operating mode (GENERATE vs MANAGE)                │
│  3. Agent.run() with Phase-specific system prompts              │
│                                                                 │
│  The loop's LLM picks internal tools:                           │
│    ├─ analyze_historical_forms     (GENERATE phase)             │
│    ├─ build_form_recommendation    (RECOMMEND phase)            │
│    ├─ update_form_recommendation   (MANAGE phase)               │
│    └─ publish_to_meta              (MANAGE phase, final action) │
└─────────────────────────────────────────────────────────────────┘
```

### File Layout

```
app/agents/adzump/agents/leadform/
├── agent.py                 LeadFormAgent (BaseAgent) + run_leadform_session
├── parent_tool.py           The ONLY orchestrator-facing entry (suggest_lead_form)
├── context.py               Phase machine definitions & prompts (STRATEGY, ANALYZE, RECOMMEND, MANAGE)
├── models.py                Pydantic domain models (LeadFormRecommendation, ContextCard, etc.)
│                            Enforces strict Meta character limits and regex constraints.
├── subagent_event_stream.py LeadFormEventStream (Telemetry passthrough, UI error forwarding)
├── tools.py                 Internal LLM tools for GENERATE (analyze, build)
├── manage_tools.py          Internal LLM tools for MANAGE (update, publish, cover photo upload)
├── parser.py                Utility to parse raw Meta Graph API responses into historical profiles
├── utils.py                 Serialization helpers for the final Meta API payload
└── AGENT.md                 This file
```

---

## Provider Configuration

The Lead Form agent uses its own isolated LLM configuration defined in `agent.py`:

| Constant | Default | Notes |
|---|---|---|
| `LEADFORM_PROVIDER` | `"deepseek"` | Used for all Lead Form reasoning and tool execution. |
| `TIER` | `"balanced"` | Standard operating tier for cost/speed efficiency. |

If the LLM struggles with complex Meta schema constraints (e.g., failing to respect character limits), you can override this in `agent.py` to use `"anthropic"` (Claude). 

---

## State & Memory Management

**CRITICAL:** The sub-agent operates on a `BaseSession` loaded from the database using `actual_session_id`. 
During `MANAGE` mode, the agent safely merges new data from the parent orchestrator using `.update()`:
```python
session.context.update({k: v for k, v in parent_ctx.items() if k != "craft_id"})
```

### Single Source of Truth
To prevent silent publishing bugs (where the UI preview doesn't match the final Meta payload), the `publish_to_meta` tool exclusively reads from `session.context.get("business_context")`. It does *not* dynamically rebuild the context from raw `product_data`, ensuring that any mid-session mutations to the website URL or privacy policy are strictly respected.

---

## SSE Events & Telemetry

The `LeadFormEventStream` (`subagent_event_stream.py`) intercepts the sub-agent's event stream and routes it to the parent stream.

- **Forwarded:** `tool_start`, `tool_result`, `data` (UI rendering payloads), `thinking`, `text` (conversational responses from the Lead Form agent are streamed to the user in real time).
- **Dropped:** `keepalive`.
- **Errors:** Forwarded actively to the parent (`await self._parent.emit_error()`) so the UI receives rich telemetry (e.g., Anthropic API crashes) instead of swallowing them silently.

---

## Error Handling & Fallbacks

- **Privacy Policy Hallucinations:** The LLM is strictly prohibited from inventing URLs. In `tools.py`, `_build_form_recommendation` programmatically overwrites the LLM's `privacy_policy.url` with the verified URL from the `business_context`.

---

## Meta API Constraints Enforced by Pydantic

Meta Instant Forms are unforgiving. `models.py` strictly enforces these before the LLM's payload ever touches the Graph API:
- `name` / `question_page_headline` / `context_card.title` / `thank_you_headline`: ≤ 60 chars.
- `thank_you_description`: ≤ 350 chars.
- `context_card.content`: max 5 bullets, ≤ 80 chars each.
- `custom_questions`: max 15. `MULTIPLE_CHOICE` must have ≥ 2 options.

If the LLM violates these, `model_validate` throws a clear `ValueError`, which the agent sees as a failed `tool_result`, allowing it to retry or fail gracefully with an explanation to the user.

---

## Image Uploads (Cover Photos)

The user can attach an image in the chat to use as the form's background. 
`update_form_recommendation` detects this attachment and dynamically uses `meta_lead_forms_adapter.upload_cover_photo` (via `multipart/form-data` HTTP POST) to push the unpublished image to the Facebook Page, retrieving a `photo_id` to inject into the `ContextCard` payload.

---

## Unsupported Features: Conditional Questions

Based on extensive API auditing (August 2026), **Meta has fully deprecated the API creation path for conditional questions** (`/{page_id}/leadgen_conditional_questions_group`).

### Official Meta Documentation
- **API Form Creation Guide:** [Lead Forms for Ads](https://developers.facebook.com/docs/marketing-api/guides/lead-ads/create)
- **Deprecation Changelog (April 30, 2019):** [API v3.3 Endpoint Deprecations](https://developers.facebook.com/docs/graph-api/changelog/4-30-2019-endpoint-deprecations)
- **Current Field Reference:** [Page/leadgen_forms](https://developers.facebook.com/docs/graph-api/reference/page/leadgen_forms/)
- **UI Creation Guide (Manual CSV Upload):** [Business Help Center](https://www.facebook.com/business/help/154286325106161)

### Agent Guidelines for Conditional Questions
- **Creation is UI-Only:** It is impossible to programmatically create conditional questions. To use them, users must manually upload a CSV file inside the Meta Ads Manager UI.
- **Agent Capability:** The Lead Form Agent **must not** attempt to automatically generate or publish conditional questions via the API. If required, the agent could generate the properly formatted CSV for the user, but the final upload is strictly a manual human step.
- **Read-Back Works:** The API *can* still read back existing conditional structures (`dependent_conditional_questions`, `conditional_questions_choices`) from older or manually created forms.
- **Lead Filtering ("Conditional Logic" Toggle):** Meta's newer lead filtering feature (which routes leads based on answers) has **zero API exposure** for reading or writing. Treat any future need for it as UI-only.
