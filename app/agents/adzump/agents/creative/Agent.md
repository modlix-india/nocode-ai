# Creative Agent

> **Status: implemented 2026-07-16** — conversational image generation via Gemini multi-turn.

## Purpose

Gathers image requirements from the user and hands off to the `ImageAgent` for actual generation. The CreativeAgent LLM (Anthropic/OpenAI) handles natural conversation — asking about aspect ratios, number of images, brand assets — while the `ImageAgent` (Gemini) handles the actual image creation and editing.

## Architecture

```
User → AdzumpAgent → manage_creatives → CreativeAgent.handle() → ImageAgent.handle()
                                                                      │
                                                                      ▼
                                                              Gemini Imagen
                                                          (multi-turn chat)
```

| Component | Role | Provider | Tools |
|-----------|------|----------|-------|
| `CreativeAgent` | Requirements gathering, session routing | Anthropic/OpenAI | `[manage_creatives]` |
| `ImageAgent` | Multi-turn image gen/editing | Gemini Imagen | None |

### File layout

```
app/agents/adzump/agents/creative/
├── __init__.py
├── AGENT.md              this file
├── agent.py              CreativeAgent (BaseAgent)
├── context.py            System prompt builder
├── models.py             (simplified — old models removed)
├── prompts/
│   └── system.txt        Requirements-gathering system prompt
└── tools/
    ├── __init__.py        CREATIVE_TOOLS = [manage_creatives]
    └── manage_creatives.py  Internal tool: creates/resumes ImageChatSessions
```

## Provider configuration

| Constant | Default | Used by | Set in |
|---|---|---|---|
| `CREATIVE_PROVIDER` | `ADZUMP_PROVIDER` or `LLM_PROVIDER` | CreativeAgent LLM loop | `agent.py` |
| `CREATIVE_MODEL_TIER` | `balanced` | CreativeAgent | `agent.py` |

The CreativeAgent passes through `get_llm_provider()`, so supports: `anthropic`, `openai`, `deepseek`, `minimax`.

## How it works

### Creating images

```
User: "Create 3 ad images: mountain sunset, sports car, beach"
  │
  ▼
CreativeAgent: "Sure! What aspect ratios? Do you have a brand logo?"
  │
User: "All 16:9, outdoor brand, here's my logo"
  │
  ▼
CreativeAgent LLM calls manage_creatives(
    user_message="Create a mountain sunset ad with outdoor brand feel",
    aspect_ratio="16:9",
    brand_logo_url="https://cdn/logo.png"
)
  │
  ▼
manage_creatives tool:
  1. Creates ImageChatSession (new BaseSession)
  2. Calls ImageAgent.handle(user_message, image_session, brand_logo_url)
  3. ImageAgent.run() → Gemini Imagen → text + image
  4. Image uploaded to CDN → _current_image_url set
  5. Returns ToolResult(success=true, summary="Generated: https://cdn/...")

CreativeAgent shows the result to user, then calls manage_creatives
again for the next image (sports car, beach) — each with its own
isolated ImageChatSession.
```

### Editing existing images

```
User: "Make the car red"
  │
  ▼
CreativeAgent LLM: maps "the car" → "img_2" (from _image_sessions context)
  │
  ▼
CreativeAgent LLM calls manage_creatives(
    user_message="Make the car red",
    image_id="img_2"
)
  │
  ▼
manage_creatives tool:
  1. Loads existing ImageChatSession for img_2
  2. Full history (first prompt + original image + this edit) sent to Gemini
  3. Gemini returns updated red car image
  4. Upload, store new URL, emit preview
```

### Multi-image parallel creation

```python
tasks = [
    ImageAgent.handle("mountain sunset", session_1, logo),
    ImageAgent.handle("sports car", session_2, logo),
    ImageAgent.handle("beach", session_3, logo),
]
results = await asyncio.gather(*tasks, return_exceptions=True)
```

Each image has its own isolated `ImageChatSession` — parallel is safe.

## SSE events

```
event: agent_started
data: {"agent_id": "creative_agent", "label": "Creative Designer"}

event: text
data: {"content": "Sure! I'll help with those images. What aspect ratios?"}

event: tool_start
data: {"id": "tc_1", "tool": "manage_creatives",
       "input": {"user_message": "Create a mountain sunset ad...",
                 "aspect_ratio": "16:9", "brand_logo_url": "..."}}

event: agent_started
data: {"agent_id": "image_agent", "label": "Image Designer"}

event: text
data: {"content": "Here's your mountain sunset image..."}

event: image_generated
data: {"url": "https://cdn/images/abc123/sunset_1.png"}

event: agent_finished
data: {"agent_id": "image_agent", "status": "success"}

event: tool_result
data: {"id": "tc_1", "tool": "manage_creatives", "success": true,
       "summary": "Generated: https://cdn/images/abc123/sunset_1.png"}

event: agent_finished
data: {"agent_id": "creative_agent", "status": "success"}

event: done
data: {"session_id": "..., "usage": {...}}
```

## The `manage_creatives` tool (internal)

Called by the CreativeAgent LLM when image generation or editing is needed.

| Param | Type | Description |
|---|---|---|
| `user_message` | string, **required** | Image prompt or edit instruction |
| `image_id` | string, optional | Existing image ID to edit (e.g. `img_1`). Omit to create new. |
| `aspect_ratio` | string, optional | `1:1`, `16:9`, `4:5`, `9:16`. Default `1:1`. |
| `brand_logo_url` | string, optional | URL of brand logo to include as reference. |

Per-image sessions stored in `session.context["_image_sessions"]`:

```python
{
    "img_1": {"session_id": "ses_abc", "aspect_ratio": "16:9", "status": "done", "image_count": 1},
    "img_2": {"session_id": "ses_def", "aspect_ratio": "1:1", "status": "done", "image_count": 2},
}
```

## Session management

- Each image has its own `ImageChatSession` (wrapping a `BaseSession`) with isolated conversation history.
- The CreativeAgent has its own sub-session (`_creative_session_key`) for requirements-gathering conversation.
- All sessions share the parent context dict for auth, tool context.

## Error handling

| Failure | What happens |
|---|---|
| Empty `user_message` | `ToolResult(success=False, error="requires a user_message")` |
| CDN upload fails | `_on_image_generated` returns `None`; image_url not set |
| Gemini API timeout | 3 retries with 2s delay; then returns error |
| Gemini returns text only (no image) | Agent responds with text normally, no image_chunk yielded |

## Testing

```
python -m pytest tests/agents/adzump/
```

E2E test covers: create → edit → done flow across multiple image sessions.

## Recent Architecture Changes (2026-07-16)

### 1. Orchestrator Loop Fix (`next_action.py`)
The orchestrator (`next_action.py`) previously expected the text-based `ad_copy` to be generated before moving to the Review phase. For image-first workflows, this caused an infinite loop because `ad_copy` is never generated. The `next_action.py` state machine was updated to track the completion of `image_sessions` directly and correctly trigger the review/publish flow for image campaigns.

### 2. Database Image Injection (Base Images)
The `CreativeAgent` was updated to fetch available product images (ranked by the `VisionAnalyst` during the web scraping phase) from the database context. 
- It extracts `creatives_with_role` and injects them into the LLM system prompt as `Available Product Images`.
- The LLM dynamically matches the user's prompt to the correct image (e.g. mapping "show me the pool" to the image with role "amenity").
- The selected URL is passed via a new `base_image_url` parameter in `manage_creatives` down to the `ImageAgent` to seed the first generation, preventing Gemini from starting from a blank hallucinated canvas.

### 3. Stateful Image Agent Refactor (Earlier Changes)
Previously, image generation used stateless, legacy tools (`generate` and `edit` methods in `creative_providers.py`). This was entirely rewritten into an orchestratable `ImageAgent` to handle stateful, multi-turn editing sessions.
- **`app/core/session.py`**: Updated to support multimodal message histories. `persist_turn` can now store structured content blocks (like `image_source` URLs) instead of just plain strings.
- **`app/core/agent.py`**: The `BaseAgent.run` loop was updated to accept `image_blocks` and attach them to the current user message, allowing any agent to pass visual context generically.
- **`app/services/creative_providers.py`**: The `GeminiImagenProvider` was completely rewritten. It now implements `stream_completion_with_tools`, which accepts the entire conversational history. It automatically intercepts `image_source` URLs, downloads them into memory (`_download_and_encode`), converts them to base64 `inline_data`, and posts the full context to Gemini. This is what enables the multi-turn conversational editing capability.
- **`app/agents/adzump/next_action.py`**: The orchestrator was shifted to route via `CreativeAgent` and track isolated `image_sessions` instead of relying on the legacy stateless tools.
