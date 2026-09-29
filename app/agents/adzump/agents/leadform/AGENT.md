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
├── __init__.py              Subsystem initialization
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
| `LEADFORM_MODEL_TIER` | `"balanced"` | Standard operating tier for cost/speed efficiency. |

If the LLM struggles with complex Meta schema constraints (e.g., failing to respect character limits), you can override this in `agent.py` to use `"anthropic"` (Claude). 

---

## State & Memory Management

**CRITICAL:** The sub-agent operates on a `BaseSession` loaded from the database using `actual_session_id`.
To prevent unbounded context growth and memory bloat, `agent.py` caps the transcript window (`_MAX_SUBAGENT_MESSAGES = 6`) and copies only curated keys from the parent context:
```python
for k in _LEADFORM_CONTEXT_KEYS:
    if k in parent_ctx:
        session.context[k] = parent_ctx[k]
```

### Single Source of Truth
To prevent silent publishing bugs (where the UI preview doesn't match the final Meta payload), the `publish_to_meta` tool exclusively reads from `session.context.get("business_context")`. It does *not* dynamically rebuild the context from raw `product_data`, ensuring that any mid-session mutations to the website URL or privacy policy are strictly respected.

---

## SSE Events & Telemetry

The `LeadFormEventStream` (`subagent_event_stream.py`) intercepts the sub-agent's event stream and routes it to the parent stream.

- **Forwarded:** `tool_start`, `tool_update`, `tool_result`, `data` (UI rendering payloads), `thinking`, `text` (conversational responses from the Lead Form agent are streamed to the user in real time), `craft`, and `craft_text` (form preview card rendering).
- **Dropped:** `done`, `keepalive`, `suggestions`, and `feedback_request` (owned and coordinated exclusively by the parent orchestrator).
- **Errors:** Forwarded actively to the parent (`await self._parent.emit_error()`) so the UI receives rich telemetry (e.g., Anthropic API crashes) instead of swallowing them silently.

---

## Error Handling & Fallbacks

- **Privacy Policy Hallucinations:** The LLM is strictly prohibited from inventing URLs. In `tools.py`, `_build_form_recommendation` programmatically overwrites the LLM's `privacy_policy.url` with the verified URL from the `business_context`.

---

## Meta API Constraints Enforced by Pydantic

Meta Instant Forms are unforgiving. `models.py` strictly enforces these before the LLM's payload ever touches the Graph API:
- `name` / `question_page_headline` / `context_card.title` / `thank_you_headline`: ≤ 60 chars (Meta Graph API limit).
- `thank_you_description`: ≤ 350 chars (Meta Graph API limit).
- `context_card.content`: max 5 bullets, ≤ 80 chars each (internal UX guardrail and char limit).
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

---

## Custom Notices & Legal Disclaimers

Meta allows advertisers to include a `custom_disclaimer` object on Instant Forms (`/{page_id}/leadgen_forms`) for legal notices, regulatory disclosures, and terms of service.

### Current Implementation (Dynamic Titles)
- **Supported Structure:** The system serializes `custom_disclaimer` with a dynamic `title` and a `body.text`:
  ```json
  "custom_disclaimer": {
    "title": "Terms & Conditions",
    "body": {
      "text": "By submitting this form, you agree to our terms of service."
    }
  }
  ```
- **Dynamic Title Control:** `custom_disclaimer_title` defaults to `"Disclaimer"` and is capped at 60 characters (UI/UX standard), but can be dynamically edited by the agent or advertiser to match specific regulatory contexts (e.g., *"Terms & Conditions"*, *"RERA Disclosure"*, *"Loan Notices"*).

### Checkboxes: API Specification vs. Regulatory Context
- **Meta API Requirement:** **Checkboxes are NOT mandatory.** Meta's Graph API fully validates and creates lead forms with only `title` and `body.text`.
- **Regulatory Use Case:** Interactive consent checkboxes (`custom_disclaimer.checkboxes`) are only required by specific regional regulations or industries:
  - **GDPR / ePrivacy (EU/UK):** Unbundled, active affirmative consent (cannot be pre-checked).
  - **TCPA / FCC (US):** Prior express written consent for automated SMS marketing or robocalls.
- **Future Implementation Roadmap:**
  If advertisers in regulated sectors require interactive checkboxes, the system can be extended by adding:
  ```python
  class DisclaimerCheckbox(BaseModel):
      text: str = Field(..., max_length=150)
      is_required: bool = Field(default=True)
      is_checked_by_default: bool = Field(default=False)
  ```
  And mapping `checkboxes` into the Meta Graph API payload in `utils.py`.

---

## Market Scope & Regional Targeting (India-First Architecture)

The current production deployment is focused on the **Indian market (`IN`)**:

- **Locale & Language:** Defaults to `EN_US` (standard for English-language campaigns in India on Meta) or normalized ISO language strings when explicitly provided.
- **Question Categories:** Full native support for high-converting Indian lead capture fields:
  - `PHONE` and `PHONE_OTP` (SMS OTP verification on Meta forms)
  - `WHATSAPP_NUMBER`
  - `POST_CODE` (PIN code lookup)
  - `DATE_TIME` (appointment scheduling)
  - `STORE_LOOKUP` and `LOCAL_DEALER` (dealership and retail franchise routing)
- **Regional Exclusions:** Latin American national ID question types (`ID_AR_DNI`, `ID_CL_RUT`, `ID_CO_CC`) are intentionally omitted from `QuestionCategory` as out of scope for the Indian market.

---

## Thank You Screen CTAs & Business Phone Formatting

Meta Instant Forms allow four CTA button types on the completion screen (`thank_you_page`): `VIEW_WEBSITE`, `CALL_BUSINESS`, `WHATSAPP`, and `MESSAGE_BUSINESS`.

### Meta Graph API Phone Specification (`CALL_BUSINESS`)
When `cta_button_type` is set to `CALL_BUSINESS`, Meta strictly requires the telephone number to be separated into two fields:
- `country_code`: The international calling code without the `+` sign (e.g., `"91"` for India).
- `business_phone_number`: The national number without leading trunk zeros (e.g., `"9876543210"`).

> [!WARNING]
> Sending a monolithic E.164 string like `+919876543210` in `business_phone_number` or omitting `country_code` triggers Meta Graph API Error 192 ("Invalid phone number").

### India-Only Normalizer (`format_meta_business_phone`)
In `utils.py`, `format_meta_business_phone` is currently scoped **strictly for India (`91`)**:
- **Supported Indian Formats:** Normalizes `+919876543210`, `919876543210`, `09876543210`, raw 10-digit `9876543210`, and punctuated variants (e.g., `+91 98765-43210`) into `("91", "9876543210")`.
- **Trunk Code Stripping:** Correctly strips leading zeros or duplicate `91` prefixes to yield a clean 10-digit national number.

### Future International Roadmap
If the platform expands beyond India to international campaigns in the future, the following updates can be implemented:
1. **Dynamic Country Code:** Derive `default_country_code` dynamically from `campaign_spec["country"]` (e.g., US `1`, UK `44`, UAE `971`) rather than hardcoding `"91"`.
2. **E.164 Dialing Code Detection:** Extend `format_meta_business_phone` to match multi-country calling code prefixes when provided with a `+` sign.

### Messenger & WhatsApp Flag Injection (`H11`)
When the CTA button is `WHATSAPP` or `MESSAGE_BUSINESS`, `serialize_leadform_payload` automatically injects `enable_messenger: true` into `thank_you_page`. Without this flag, Meta rejects forms configured with messaging CTAs with a 400 Bad Request error.

### Unsupported CTA: DOWNLOAD (Missing Gated File Pipeline)
Meta Instant Forms offer a `DOWNLOAD` completion button intended for distributing resources such as brochures, whitepapers, or ebooks. However, Meta's Graph API requires a **`gated_file`** asset parameter (an uploaded document/PDF asset ID) when this CTA type is chosen.

- **Current System Constraint:** The current prompt component and UI only support image uploads (strictly for form cover photos). The platform does not yet have a document/PDF upload pipeline or an adapter integration to register file assets with Meta.
- **Agent Menu Guardrail:** To prevent the LLM from suggesting a completion action that cannot be fulfilled (which would leave users with a broken download button or trigger Meta publishing errors), `DOWNLOAD` is intentionally excluded from the active CTA recommendation menu in `context.py`.
- **Future Implementation Roadmap:**
  1. Add PDF/document upload support to the prompt/UI component.
  2. Implement an adapter upload method (`meta_lead_forms_adapter.upload_gated_document`) to obtain a Meta `file_id`.
  3. Pass and serialize `gated_file: file_id` in `thank_you_page` inside `utils.py`.


