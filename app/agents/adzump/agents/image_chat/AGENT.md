# Image Agent

> **Status: implemented 2026-07-16** — multi-turn conversational image generation via Gemini Imagen.

## Purpose

Pure conversational image generation backed by Gemini (`gemini-3.1-flash-image-preview`). Generates and edits images through natural multi-turn conversation — no function-calling, no tools, just text + image round-trips.

## Architecture

```
CreativeAgent → ImageAgent.handle(user_message, image_session, ...)
                     │
                     ▼
           GeminiImagenProvider.stream_completion_with_tools()
                     │
          ┌──────────┼──────────┐
          ▼          ▼          ▼
      text_delta  image_chunk   done
          │          │
          │          ▼
          │    _on_image_generated()
          │    ┌─ Upload to CDN
          │    └─ Emit image_generated SSE event
          │
          ▼
    append to session.messages
    as {"type": "image_source", "url": "https://cdn/..."}
```

### Key properties

| Property | Value |
|---|---|
| Provider | `GeminiImagenProvider` (`gemini-3.1-flash-image-preview`) |
| `max_turns` | 1 (single call per `handle()` — multi-turn achieved by caller) |
| Tools | None |
| `supports_vision` | True |
| `supports_prompt_caching` | False |

### File layout

```
app/agents/adzump/agents/image_chat/
├── __init__.py
├── AGENT.md              this file
├── agent.py              ImageAgent (BaseAgent)
├── context.py            System prompt builder
├── models.py             ImageChatSession dataclass
└── prompts/
    └── system.txt        Image generation system prompt
```

## How it works

### Message format (internal — stored in session)

Images stored as lightweight URL references in `BaseSession.messages`:

```python
session.messages = [
    {"role": "user", "content": [
        {"type": "image_source", "url": "https://cdn/brand_logo.png"},  # brand asset
        {"type": "text", "text": "Create a mountain sunset, outdoor brand"}
    ]},
    {"role": "assistant", "content": [
        {"type": "text", "text": "Here's your mountain sunset ad:"},
        {"type": "image_source", "url": "https://cdn/creatives/img_1.png"}  # generated
    ]},
    {"role": "user", "content": [
        {"type": "text", "text": "Make the sky more vibrant"}
    ]},
]
```

### Gemini wire format conversion

The provider (`creative_providers.py`) handles all format conversion:

| Direction | Format | Images |
|---|---|---|
| Request to Gemini | `inline_data` (snake_case) | URL → base64 via download |
| Response from Gemini | `inlineData` (camelCase) | base64 → upload to CDN → URL |
| Session storage | `image_source` (custom) | URL only (no base64) |

### Per-turn flow

```
1. CreativeAgent calls ImageAgent.handle(user_message, image_session, ...)
2. handle() appends message to session via BaseAgent.run()
3. _stream_turn → GeminiImagenProvider.stream_completion_with_tools()
4. Provider converts all image_source blocks to inline_data
5. Provider yields text_delta + image_chunk (never tool_use)
6. stop_reason = "end_turn", tool_use_blocks = [] → loop exits
7. _on_image_generated: uploads image to CDN, emits SSE preview
8. handle() returns ToolResult(image_url=...)
```

### Image upload

Generated images are uploaded to the Gateway files API:

```
POST /api/files/upload
Headers: Authorization, clientCode, appCode, Content-Type, X-Filename
Body: raw image bytes
Filename: images/{app_code}/{session_id}/{prompt_slug}_{n}.png
```

On success, `session.context["_current_image_url"]` is set and returned via `_on_image_generated`.

## Session lifecycle

Per-image isolation — each `ImageChatSession` has its own `BaseSession`:

```python
@dataclass
class ImageChatSession:
    base_session: BaseSession
    aspect_ratio: str = "1:1"
    image_count: int = 0
```

Stored by the parent CreativeAgent in `session.context["_image_sessions"]`:

```python
{
    "img_1": {
        "session_id": "ses_abc",
        "aspect_ratio": "16:9",
        "status": "done",
        "image_count": 1,
    },
}
```

## API endpoint

The ImageAgent is not exposed directly as an HTTP endpoint — it's called internally by the CreativeAgent via `ImageAgent.handle()`.

```
ImageAgent.handle(
    user_message: str,
    image_session: ImageChatSession,
    brand_logo_url: str | None = None,
    aspect_ratio: str | None = None,
    event_stream: AgentEventStream | None = None,
) -> ToolResult
```

## SSE events

```
event: agent_started
data: {"agent_id": "image_agent", "label": "Image Designer"}

event: text
data: {"content": "Here's your mountain sunset image..."}

event: image_generated
data: {"url": "https://cdn/images/abc123/sunset_1.png"}

event: agent_finished
data: {"agent_id": "image_agent", "status": "success"}
```

## Error handling

| Failure | What happens |
|---|---|
| CDN upload fails | `_on_image_generated` returns `None`; no URL set |
| Gemini API timeout | 3 retries with 2s delay; raises `RuntimeError` |
| Gemini returns text only | Only `text_delta` yielded; no `image_chunk` |
| Image download fails | `_download_and_encode` logs warning, skips the block |

## Design decisions

- **No tools** — Gemini's image model doesn't support function calling.
- **max_turns=1** — The loop is a straight-through text+image response. Multi-turn is the caller's responsibility (CreativeAgent calls `handle()` repeatedly with the same session).
- **All history replayed** — Every previous image is included in every Gemini call. No selective dropping. Estimated cost: ~258 tokens per image.
- **URL references in history** — Base64 only exists transiently during API calls. Session storage uses lightweight `image_source` blocks.
- **Per-image sessions** — Each image gets an isolated `BaseSession` so "make it brighter" maps to the right image.
- **Aspect ratio is per-session constant** — Set at creation, immutable.

## Recent Architecture Changes (2026-07-16)

### 1. HTTP Upload Timeout Fix
Large image generations (780KB+) were previously crashing due to a hardcoded 30-second `httpx.ReadTimeout` when uploading the generated images from the `ImageAgent` to the local Gateway CDN. The `timeout=30.0` inside `_upload_image` in `agent.py` was bumped to `timeout=120.0` to ensure heavy local uploads can complete without aborting the generation.

### 2. Multi-turn Image Editing (Image-to-Image Generation)
Gemini Imagen requires the actual base image to be passed within the *current* user prompt (Image-to-Image) for edits to function properly and preserve layout. Previously, edits failed because the model only saw the previous image in the `model` history context, not as an active input.
- `ImageAgent.handle()` was updated to automatically pull `_current_image_url` (the previously generated image) and explicitly append it to the current turn's `image_blocks`.
- It now also accepts a `base_image_url` to perform Image-to-Image generation on the very first turn using existing product photos, falling back to brand-new generation if none are provided.

### 3. Stateful Image Agent Refactor (Earlier Changes)
Previously, image generation used stateless, legacy tools (`generate` and `edit` methods in `creative_providers.py`). This was entirely rewritten into an orchestratable `ImageAgent` to handle stateful, multi-turn editing sessions.
- **`app/core/session.py`**: Updated to support multimodal message histories. `persist_turn` can now store structured content blocks (like `image_source` URLs) instead of just plain strings.
- **`app/core/agent.py`**: The `BaseAgent.run` loop was updated to accept `image_blocks` and attach them to the current user message, allowing any agent to pass visual context generically.
- **`app/services/creative_providers.py`**: The `GeminiImagenProvider` was completely rewritten. It now implements `stream_completion_with_tools`, which accepts the entire conversational history. It automatically intercepts `image_source` URLs, downloads them into memory (`_download_and_encode`), converts them to base64 `inline_data`, and posts the full context to Gemini. This is what enables the multi-turn conversational editing capability.
- **`app/agents/adzump/next_action.py`**: The orchestrator was shifted to route via `CreativeAgent` and track isolated `image_sessions` instead of relying on the legacy stateless tools.
