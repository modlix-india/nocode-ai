# Creative Generator Agent

## Purpose

`CreativeGeneratorAgent` is an orchestrator sub-agent for the `adzump` system, specializing in ad creative generation and modification. It generates premium quality ad copy (headlines, descriptions, call-to-actions) and pairs them with generated/fetched background images (using Imagen API) to produce final multi-format ad assets (square, portrait, landscape) for Google and Meta Ads.

Unlike main agents, the creative generator acts as a sequential pipeline service rather than a conversational tool-use loop, executing structured pipelines behind the `generate_fresh_creatives` and `modify_existing_creative` tools.

## How It Works

The creative generation process has two main entry points:

### 1. Fresh Generation Pipeline (`generate`)
Used to create brand new creatives from scratch:
1. **Copywriting**: Calls Gemini to generate compelling headlines, descriptions, CTAs, and a detailed image prompt based on the product profile and target audience.
2. **Asset Sourcing**: Searches and retrieves brand assets or stock background candidates.
3. **Image Generation**: If no suitable brand images are available, calls the Imagen API to generate a high-quality background matching the copy styling.
4. **Layout Assembly**: Composes, overlays, and renders the ad copy, CTAs, and background into a final square ad creative.

### 2. Modification & Aspect-Ratio Pipeline (`modify`)
Used to adjust, update copy, or generate alternative formats for existing creatives:
1. **Parameter Overrides**: Applies custom user changes (headline, description, CTA, theme, custom backgrounds).
2. **Aspect Ratio Adaptation**: Generates and compiles aspect ratio variations (`portrait` (9:16) and `landscape` (16:9)) from the baseline square creative.

---

## Architecture

```
app/agents/adzump/agents/creative_generator/
├── agent.py              # Orchestrator singleton exposing .generate() and .modify()
├── context.py            # System prompt and configuration definitions
├── models.py             # Typed Pydantic data schemas (AdCopy, CreativeItem, etc.)
├── config_parser.py      # Parses visual design and layout configuration schemas
│
├── fresh_generation.py   # Core logic for scratch generation (A4)
├── modification.py       # Core logic for edits and size expansion (A4)
│
├── copywriter.py         # LLM-based copy, theme, and image prompt generation
├── selection_agent.py    # Selection agent to filter and score target visual styles
├── selector.py           # Layout selector matching business domain specs
│
├── imagen_api.py         # Google Vertex AI Imagen API service client
├── image_utils.py        # Cropping, scaling, and canvas drawing utilities
│
└── prompts/              # System prompt templates
```

---

## Exposed Tools

These tools are registered under the parent `adzump` agent and delegate execution to this generator:

### `generate_fresh_creatives`
Generates ad copies and a premium square image from scratch.
* **Arguments**:
  * `custom_theme` (string, optional): Visual style direction.
  * `target_personas` (string, optional): Target demographics.

### `modify_existing_creative`
Edits text/styles or generates alternative aspect ratios (portrait, landscape) for a specific creative.
* **Arguments**:
  * `target_creative_index` (integer, required): 1-based index of the target creative.
  * `custom_headline`/`custom_description`/`custom_cta`/`custom_theme` (string, optional).
  * `target_formats` (string, optional): comma-separated formats (e.g. `'square,portrait,landscape'`).
  * `custom_background_image` (string, optional): URL or path to a custom background image.

---

## Testing

Run unit tests targeting the creative generator's components:

```bash
pytest tests/agents/adzump/agents/creative_generator
```
