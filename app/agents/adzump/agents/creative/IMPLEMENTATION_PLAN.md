# Creative Agent Implementation Plan

## Goal
Implement a robust, persistent, and visually accurate sub-agent (`CreativeAgent`) for generating ad creatives. This plan documents the implemented architecture, fixes, and workflow enhancements to finalize the creative generation pipeline.

## Implemented Changes

### 1. Core Agent Framework
- **Agent Initialization**: `CreativeAgent` inherits from `BaseAgent`.
- **Tool Access**: Connected `create_creative`, `edit_creative`, and `list_creatives` tools.
- **Context Management**: Created `build_creative_context` to inject `Product Profile` details (brand, persona, pricing, location).

### 2. Base Image Selection Upgrade
- **Issue**: The agent was frequently generating backgrounds from scratch or choosing poor images (like floor plans or standalone logos) because it only picked from the first 3 scraped assets.
- **Implementation**: 
  - Created `CreativeSelectionAgent` in `selection.py` using a fast vision model.
  - Increased candidate evaluation pool from 3 to 10 images.
  - Implemented strict scoring guidelines in `creative_selection.txt` to reject blueprints, maps, and blurry photos, preferring high-quality lifestyle/product shots.

### 3. Anti-Hallucination Logo Flow
- **Issue**: Without an actual logo image, the generative model (Gemini Imagen) would hallucinate text on the creative.
- **Implementation**: 
  - Updated `system.txt` with a CRITICAL rule: The agent must verify `logo_url` in the context.
  - If missing, the agent stops and explicitly requests the user to upload a brand logo.
  - Plumbed the logo through `tools.py` directly into the `GeminiImagenProvider` payload as `Image 1`.

### 4. UI Layout & Markdown Persistence
- **Issue**: Landscape/portrait creatives were rendering at native resolutions, breaking the chat UI.
- **Implementation**: 
  - Enforced exact markdown styling in `system.txt`: `![Preview](url){style="width: 250px; height: 250px; object-fit: contain; border-radius: 8px; margin: 4px;"}`.
  - Added strict constraints preventing the LLM from altering this syntax, ensuring the frontend markdown parser accurately styles every image type.

### 5. Session State Persistence (`core/session.py`)
- **Issue**: `Creative` objects stored in the session dictionary were converted to strings during database serialization, causing creatives to vanish upon page reload.
- **Implementation**: 
  - Added a `custom_encoder` to `BaseSession._serialize_context`.
  - The encoder dynamically checks for `.to_dict()` on objects, ensuring our `Creative` dataclasses are accurately persisted as JSON dictionaries and natively hydrated back upon session load.

## Verification
- ✅ Generation of Square, Landscape, and Portrait creatives tested.
- ✅ Vision-based base image selection validated (evaluates top 10).
- ✅ Logo requirement enforced.
- ✅ Page reload persistence verified.
- ✅ Markdown sizing parsed flawlessly by the frontend.
