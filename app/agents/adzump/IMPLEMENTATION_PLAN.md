# Implementation Plan: Conversational Image Generation with Gemini

**Status**: Draft for Review  
**Author**: AI Agent  
**Date**: 2026-07-16

---

## 1. Problem Statement

Current image generation in the adzump creative flow uses a **fake wrapper** pattern:

1. `CreativeAgent` uses Anthropic/OpenAI for conversation
2. When it needs an image, it calls `create_creative` tool → old `ImageAgent` → `GeminiImagenProvider.generate()` (single-shot REST API)
3. The conversation is **not multi-turn** — every edit requires an explicit `edit_creative` tool call
4. Gemini's native multi-turn image generation capability is unused

The result: users cannot naturally iterate on images through conversation ("make it brighter" → "add birds" → "change color" → "done") in a single continuous flow.

---

## 2. Solution Overview

Replace the single-shot REST API calls with a **true conversational ImageAgent** that uses Gemini (`gemini-3.1-flash-image-preview`) as its primary LLM provider. The agent supports multi-turn image creation, editing, and refinement through natural conversation.

### Architecture

```
User → AdzumpAgent → CreativeAgent (requirements) → ImageAgent (multi-turn Gemini)
```

| Component | Role | Provider | Tools |
|-----------|------|----------|-------|
| `CreativeAgent` | Gathers requirements, manages image sessions | Anthropic/OpenAI | `[manage_creatives]` |
| `ImageAgent` | Multi-turn image gen/editing | Gemini `gemini-3.1-flash-image-preview` | None |

---

## 3. Folder Structure

### Before

```
app/agents/adzump/agents/creative/
├── __init__.py
├── agent.py                    ← CreativeAgent (ad-copy focused, complex tools)
├── context.py
├── image_agent.py              ← Single-shot ImageAgent (TO DELETE)
├── models.py                   ← Creative, ImageBrief, ImageResult
├── selection.py                 ← Vision-based pool selection (TO DELETE)
├── prompts/
│   ├── system.txt              ← Tool-focused system prompt (TO REPLACE)
│   ├── image_layout.txt         ← Template for Gemini (TO DELETE)
│   └── creative_selection.txt   ← (TO DELETE)
└── tools/
    ├── __init__.py              ← CREATIVE_TOOLS = [create, edit, list]
    ├── create_creative.py       ← (TO DELETE)
    ├── edit_creative.py         ← (TO DELETE)
    ├── list_creatives.py        ← (TO DELETE)
    └── _shared.py               ← (TO DELETE — merge into creative_providers)

app/services/creative_providers.py  ← GeminiImagenProvider with generate/edit only
```

### After

```
app/agents/adzump/agents/creative/          ← CreativeAgent (simplified)
├── __init__.py
├── agent.py                  ← Requirements + handoff to ImageAgent
├── context.py                ← New system prompt (requirements-gathering)
├── models.py                 ← Simplified Creative model
└── tools/
    ├── __init__.py            ← CREATIVE_TOOLS = [manage_creatives]
    └── manage_creatives.py    ← NEW: orchestrates ImageAgent lifecycle

app/agents/adzump/agents/image_chat/        ← NEW: ImageAgent
├── __init__.py
├── agent.py                  ← ImageAgent (BaseAgent, no tools, Gemini provider)
├── context.py                ← System prompt for Gemini image conversation
└── models.py                 ← ImageSession, ChatResult

app/services/creative_providers.py          ← REWRITTEN
                                            ← Remove generate()/edit()
                                            ← Add chat() + stream_chat()
                                            ← Wire Gemini API format correctly
```

### Files to Delete (7 files)

| File | Reason |
|------|--------|
| `agents/creative/image_agent.py` | Replaced by `image_chat/agent.py` |
| `agents/creative/tools/create_creative.py` | Replaced by conversational flow |
| `agents/creative/tools/edit_creative.py` | Replaced by conversational flow |
| `agents/creative/tools/list_creatives.py` | Not needed |
| `agents/creative/tools/_shared.py` | Helpers moved to `creative_providers.py` |
| `agents/creative/selection.py` | Not needed |
| `agents/creative/prompts/image_layout.txt` | No template prompt needed |
| `agents/creative/prompts/creative_selection.txt` | Not needed |

---

## 4. Detailed Design

### 4.1 Message Format (Internal — Stored in Session)

Images are stored as lightweight **URL references** in session history. Base64 data only exists transiently during API calls.

```python
# Stored in BaseSession.messages (lightweight, no base64):
session.messages = [
    {"role": "user", "content": [
        {"type": "image_source", "url": "https://cdn/brand_logo.png"},  # brand asset
        {"type": "text", "text": "Create a mountain sunset, outdoor brand"}
    ]},
    {"role": "assistant", "content": [
        {"type": "text", "text": "Here's your mountain sunset ad:"},
        {"type": "image_source", "url": "https://cdn/creatives/img_1.png"}
    ]},
    {"role": "user", "content": [
        {"type": "text", "text": "Make the sky more vibrant"}
    ]},
]
```

### 4.2 Gemini Wire Format Conversion

The provider (`creative_providers.py`) handles all API format conversion:

**Request** (sent to Gemini):
```json
{
  "contents": [
    {"role": "user", "parts": [
      {"inline_data": {"mime_type": "image/png", "data": "<base64_logo>"}},
      {"text": "Create a mountain sunset, outdoor brand"}
    ]},
    {"role": "model", "parts": [
      {"text": "Here's your mountain sunset ad:"},
      {"inline_data": {"mime_type": "image/png", "data": "<base64_img1>"}}
    ]},
    {"role": "user", "parts": [
      {"text": "Make the sky more vibrant"}
    ]}
  ],
  "generationConfig": {
    "responseModalities": ["TEXT", "IMAGE"],
    "imageConfig": {"aspectRatio": "16:9"}
  }
}
```

**Response** (from Gemini):
```json
{
  "candidates": [{
    "content": {
      "parts": [
        {"text": "Here's the updated version with vibrant sky:"},
        {"inlineData": {"mimeType": "image/png", "data": "<base64_new_img>"}}
      ]
    }
  }],
  "usageMetadata": {
    "promptTokenCount": 450,
    "candidatesTokenCount": 120
  }
}
```

**Key format rules:**
- Request: `inline_data` (snake_case) — per Gemini API spec
- Response: `inlineData` (camelCase) — Google API convention
- Provider converts response `inlineData` → session `image_source` (URL)
- On next turn, provider downloads URL → converts back to `inline_data`

### 4.3 ImageAgent Agent Loop

ImageAgent extends `BaseAgent` with **no tools**:

```
ImageAgent.__init__:
  - name = "image_chat"
  - tools = []               (no function calling — pure chat)
  - provider = "gemini_imagen" (GeminiImagenProvider)
  - max_turns = 1            (1 user message per handle() call)

Flow per handle() call:
  1. CreativeAgent calls ImageAgent.handle(user_message, image_session, ...)
  2. ImageAgent.run() is called
  3. _stream_turn → GeminiImagenProvider.stream_completion_with_tools()
     → yields text_delta + image_chunk (never tool_use)
  4. stop_reason = "end_turn", tool_use_blocks = [] → loop exits after 1 turn
  5. Image uploaded via _on_image_generated hook
  6. handle() returns ToolResult(image_url=...)

Multi-turn is achieved by CreativeAgent calling handle() repeatedly
with the same image_session — the provider replays the full history
to Gemini each time.
```

### 4.4 `BaseAgent._stream_turn` Changes

Add a single hook that subclasses override:

```python
# In BaseAgent (default: no-op)
async def _on_image_generated(self, image_data: bytes, mime_type: str,
                                session, event_stream) -> str | None:
    """Override in subclasses to upload + emit preview."""
    return None
```

In `_stream_turn`, handle `image_chunk` type:

```python
elif chunk.type == "image_chunk":
    url = await self._on_image_generated(
        chunk.image_data, chunk.image_mime, session, event_stream
    )
    if url:
        session.context["_current_image_url"] = url
```

### 4.5 `StreamChunk` Extension

Add `image_chunk` type (in `app/services/llm_provider.py`):

```python
@dataclass
class StreamChunk:
    type: str          # ... + "image_chunk"
    image_data: bytes = b""      # NEW
    image_mime: str = ""          # NEW
    image_prompt: str = ""        # NEW
    # ... existing fields unchanged
```

### 4.6 Session Lifecycle

#### Per-Image Isolation

```python
# Stored in CreativeAgent's session context:
session.context["_image_sessions"] = {
    "img_1": {
        "session_id": "ses_abc123",     # BaseSession ID for ImageAgent
        "aspect_ratio": "16:9",
        "status": "done",
        "current_image_url": "https://cdn/img_1.png",
    },
    "img_2": {
        "session_id": "ses_def456",
        "aspect_ratio": "1:1",
        "status": "generating",
        "current_image_url": None,
    }
}
```

Each image gets its own `BaseSession` with isolated `messages[]` history. The ImageSession's BaseSession is persisted in the DB independently.

#### Step-by-Step: Creating an Image

```
1. User: "Create 3 ad images: mountain sunset, sports car, beach"
2. CreativeAgent (Anthropic): "What aspect ratios? What brand?"
3. User: "All 16:9, outdoor brand, green logo"
4. CreativeAgent calls manage_creatives(user_message="mountain...", 
                                         image_id=None, aspect="16:9")
   a. Creates BaseSession("image_chat") → ses_abc
   b. Initial content: [image_source(logo_url), text("mountain sunset...")]
   c. ImageAgent.handle() → Gemini → image → upload → return
   d. Stores: image_sessions["img_1"] = {session_id: "ses_abc", ...}
   e. REPEAT for img_2 (ses_def) and img_3 (ses_ghi) via asyncio.gather
5. All 3 images displayed to user
```

#### Step-by-Step: Editing an Image

```
1. User: "Make the car red"  
2. CreativeAgent (Anthropic): reads _image_sessions → "car" → "img_2"
3. Calls manage_creatives(user_message="Make the car red", image_id="img_2")
   a. Loads BaseSession("image_chat") ses_def
   b. ses_def.messages contains full history: [car prompt, original image, ...]
   c. Provider downloads current image from CDN
   d. Gemini receives: [car prompt + logo, original image, "make the car red"]
   e. Gemini returns updated red car image
   f. Upload, store new URL, emit preview
```

#### Session Switch Mid-Edit

```
User: "Actually switch back to the mountain and add snow to the peaks"
→ CreativeAgent maps "mountain" → "img_1"
→ manage_creatives(user_message="add snow to peaks", image_id="img_1")
→ Loads img_1's session, sends full history + "add snow" to Gemini
→ Gemini sees mountain's full context → adds snow correctly
```

### 4.7 Brand Assets & Logo Handling

**Path B — Inline from the start**: The brand logo is downloaded by CreativeAgent and included as an `image_source` block in the ImageAgent's **first user message**:

```python
initial_content = [
    {"type": "image_source", "url": logo_download_url},  # brand logo
    {"type": "text", "text": "Create a mountain sunset ad, outdoor brand"}
]
```

The provider converts all `image_source` blocks to `inline_data` at API call time.

**All history replayed**: Every previous image in the session (including the logo) is included in every Gemini call. Cost analysis:

| Turns | All images @ ~258 tokens each | Text | Total | Context % of 128K |
|-------|-------------------------------|------|-------|-------------------|
| 5     | 5 × 258 = 1,290 | ~500 | ~1.8K | 1.4% |
| 20    | 20 × 258 = 5,160 | ~2K | ~7K | 5.5% |
| 100   | 100 × 258 = 25,800 | ~10K | ~36K | 28% |

Even at 100 turns, context usage is well within limits.

### 8. Multi-Image Creation

**Parallel for creation, sequential for editing:**

```python
# CreativeAgent creates multiple images in parallel
tasks = [
    ImageAgent.handle(prompt_1, session_1, ...),
    ImageAgent.handle(prompt_2, session_2, ...),
    ImageAgent.handle(prompt_3, session_3, ...),
]
results = await asyncio.gather(*tasks, return_exceptions=True)
```

Each `ImageAgent.handle()` call is independent (different BaseSession), so parallel is safe.

---

## 5. API Changes

### 5.1 New/Modified Files

| File | Action | Description |
|------|--------|-------------|
| `app/services/llm_provider.py` | **Modify** | Add `image_chunk` to `StreamChunk` dataclass |
| `app/services/creative_providers.py` | **Rewrite** | Replace `generate()`/`edit()` with `chat()` + `stream_chat()`; add `_convert_messages()` with proper `inline_data`/`inlineData` handling |
| `app/core/agent.py` | **Modify** | Add `_on_image_generated()` hook in `BaseAgent`; handle `image_chunk` in `_stream_turn` |
| `app/agents/adzump/agents/image_chat/__init__.py` | **Create** | Empty |
| `app/agents/adzump/agents/image_chat/agent.py` | **Create** | `ImageAgent(BaseAgent)` — no tools, Gemini provider, `_on_image_generated` override |
| `app/agents/adzump/agents/image_chat/context.py` | **Create** | System prompt for image generation chat |
| `app/agents/adzump/agents/image_chat/models.py` | **Create** | `ImageSession`, `ChatResult` dataclasses |
| `app/agents/adzump/agents/creative/tools/manage_creatives.py` | **Create** | Handoff tool — creates/resumes ImageAgent sessions |
| `app/agents/adzump/agents/creative/tools/__init__.py` | **Modify** | Export only `manage_creatives` |
| `app/agents/adzump/agents/creative/agent.py` | **Simplify** | Remove old Creative model management; keep requirements gathering + handoff |
| `app/agents/adzump/agents/creative/context.py` | **Rewrite** | New system prompt centered on requirements gathering |
| `app/agents/adzump/agents/creative/models.py` | **Simplify** | Strip `ImageBrief`, `ImageResult`; keep minimal `Creative` |

### 5.2 Deleted Files (8 files)

See Section 3 above.

---

## 6. Key Design Decisions

### 6.1 Why Not One Agent with Both Capabilities?

The user specifically requested separation:
- **CreativeAgent**: Requirements gathering (uses Anthropic/OpenAI for general conversation)
- **ImageAgent**: Multi-turn image generation (uses Gemini for native image support)

Gemini's image model (`gemini-3.1-flash-image-preview`) does not support function calling, so it can't use tools. Keeping them separate avoids this limitation.

### 6.2 Why BaseAgent for ImageAgent (Not a Custom Loop)?

BaseAgent provides for free:
- Session management + persistence (`BaseSession`)
- Token tracking (`accumulate_usage`, `record_token_usage`)
- SSE streaming protocol (`emit_tool_start/result`, `emit_text`)
- Sub-agent lifecycle (`agent_started`/`agent_finished` cards in UI)
- Message history management

The only "wasted" feature is tool-use loop, which is trivially bypassed with `max_turns=1`.

### 6.3 Why Not Selective Image Replay?

Sending ALL images in history context costs ~258 tokens per image. At 100 turns = 28% of 128K context. Well within limits. Selective replay adds complexity for marginal gain. If context pressure becomes an issue later, images can be dropped from the middle (keep first + last N).

### 6.4 Why Per-Image Sessions?

Each image needs isolated conversation history so Gemini correctly maps edits to the right image. Without isolation, "make it brighter" in a shared session would be ambiguous (which image?).

### 6.5 Token Tracking

Gemini returns `usageMetadata` in every response:
```json
{"usageMetadata": {"promptTokenCount": 450, "candidatesTokenCount": 120}}
```

These are parsed into `StreamChunk(type="done", usage={...})`, which BaseAgent's `_stream_turn` already handles via `session.accumulate_usage()` and `session.record_token_usage()`. No changes needed to the tracking pipeline.

---

## 7. Configuration

No new config variables needed. Existing `GOOGLE_API_KEY` is reused. The model name `gemini-3.1-flash-image-preview` is hardcoded in `creative_providers.py` (unchanged from current).

---

## 8. Open Questions for Discussion

### Q1: Tool naming

The `manage_creatives` tool is intended to be forward-compatible for video. Should the tool still be called `manage_creatives` in the adzump tool registry (line 30 of `tools/creative_generation.py`)? This keeps the existing `REGISTRY_TOOLS` entry unchanged.

**Recommendation**: Keep `manage_creatives` — the `user_message` can describe anything (image, video, audio in the future).

### Q2: ImageAgent system prompt depth

How detailed should the ImageAgent's system prompt be? Options:
- **Minimal**: "You generate images through conversation. Use Gemini's image capabilities."
- **Detailed**: Include style guidelines, brand voice rules, compliance rules (RERA, etc.)

**Recommendation**: Minimal first iteration. Add brand/compliance rules to CreativeAgent (which passes them in `user_message`), not ImageAgent's system prompt.

### Q3: Image upload folder

The existing code uploads generated images to `creatives/` folder via `_IMAGE_KIND_FOLDERS` in `_shared.py`. Should ImageAgent use a different folder (`image_chat/`) for the new conversational flow?

**Recommendation**: Same `creatives/` folder — it's already the image storage location.

### Q4: First image generation — with or without brand logo

When the user hasn't provided any brand assets, should ImageAgent generate the image from scratch (no logo inline) or require a logo first?

**Recommendation**: From scratch. The CreativeAgent can ask for brand assets if needed, but ImageAgent should work without them.

### Q5: Testing strategy

How should this be tested?
- **Unit**: Test `creative_providers.py` message conversion (`_convert_messages`)
- **Integration**: Test full ImageAgent flow with actual Gemini API (in CI with API key)
- **E2E**: Test via adzump chat endpoint

**Recommendation**: Unit + integration tests for `creative_providers.py`. E2E test for a complete create → edit → done flow.

---

## 9. Implementation Order

1. **`llm_provider.py`**: Add `image_chunk` to `StreamChunk`
2. **`creative_providers.py`**: Rewrite — implement `stream_completion_with_tools` with proper format conversion and image handling
3. **`core/agent.py`**: Add `_on_image_generated` hook; handle `image_chunk` in `_stream_turn`
4. **`image_chat/`**: Create models, context, agent files
5. **`creative/tools/manage_creatives.py`**: New handoff tool
6. **`creative/agent.py`**: Simplify CreativeAgent
7. **`creative/context.py`**: Rewrite system prompt
8. **`creative/models.py`**: Simplify models
9. **`creative/tools/__init__.py`**: Update exports
10. **Delete**: Old files (8 files)
11. **Test**: Unit + integration + E2E

---

## 10. Edge Cases & Risks

| Edge Case | Handling |
|-----------|----------|
| Gemini API returns text only (no image) | Provider yields only `text_delta`, no `image_chunk`. Agent responds with text normally. |
| Gemini API returns multiple images | Provider yields multiple `image_chunk` events → all uploaded and displayed |
| CDN upload fails | `_on_image_generated` returns `None`, `_current_image_url` not set. Agent proceeds without image preview. |
| User switches topics mid-image | CreativeAgent detects and stops calling `manage_creatives` for that session. Session remains dormant. |
| Gemini rate limit (300 RPM) | `_post_gemini` has 3 retry attempts with 2s delays. No further throttling. |
| Large history (>100 turns) | 128K context window. At worst-case 28% usage. If exceeded, Gemini returns error → retry fails → user notified. |
| Empty reference_images param | Handled by `manage_creatives` default empty list — no-op. |

---

## 11. Success Criteria

1. User can create an image through conversation ("Create a sunset mountain scene, 16:9")
2. User can edit the image through natural language ("Make the sky more orange")
3. User can create a second image in the same session and edit that one independently
4. All generated images are uploaded to CDN and displayed in chat
5. Token usage is tracked correctly for Gemini calls
6. The CreativeAgent correctly routes messages to the right image session
7. Old `create_creative`/`edit_creative` tools are removed without breaking the parent adzump agent
