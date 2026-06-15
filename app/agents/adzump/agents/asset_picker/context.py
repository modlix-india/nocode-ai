"""System prompt for AssetPickerAgent.

The prompt lives in ``agents/product/prompts/product_assets.txt`` today.
The agent reads it from there to preserve byte-for-byte parity with the
existing direct-call shape. A v2 refactor can move the file under
``agents/asset_picker/prompts/``.

The prompt is extended at build time with an explicit JSON-shape contract.
The original direct call enforced output shape via OpenAI's strict
``response_format`` parameter; running through BaseAgent's loop means we
fall back to JSON-in-prompt + Pydantic parse on the way out.
"""

from __future__ import annotations

from pathlib import Path

from app.core.context import BaseContext


# DRAFT-NOTE · prompt currently lives in the product agent's prompt folder.
# When we move it (D3 in implementation-notes.md), change this path.
_PICKER_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "product" / "prompts" / "product_assets.txt"
)


# Output-contract suffix. The original code got this for free from
# ``response_format=_AssetSelection``; without that, the model needs an
# explicit JSON shape instruction. Keep it short — the heavy prompt-
# engineering already lives in product_assets.txt.
_JSON_OUTPUT_CONTRACT = """

## Output contract

Your FINAL message MUST be a single fenced ```json block and nothing else.
The schema is:

```json
{
  "logos": [
    {
      "idx": 0,                       // index into the candidate list
      "role": "developer",            // "developer" | "project" | "cobrand" | "main" — short label, may be ""
      "reasoning": "one sentence",
      "background_hint": "light"      // "light" | "dark" | "" — UI tile contrast from the thumbnail
    }
  ],
  "creatives": [
    {
      "idx": 3,                       // index into the candidate list
      "role": "hero",                 // "hero" | "amenity" | "floor_plan" | "unused" — MUST be one of these four exactly
      "reasoning": "main exterior shot" // <= 120 chars
    },
    {"idx": 7, "role": "amenity", "reasoning": "pool"},
    {"idx": 12, "role": "floor_plan", "reasoning": "2BHK unit plan"}
  ],
  "confidence": 0.85,                 // 0.0..1.0 self-assessed precision on logos
  "note": "string or empty"           // ONE sentence if logos=[] OR a logo-looking candidate was rejected
}
```

Hard caps: at most 3 logos. Use empty list/string/0 when there's no signal — do not invent.
**Always emit `creatives` (the role-tagged list), NOT a flat `creative_idxs` array** — the older shape is deprecated and produces empty roles downstream.
"""


def _load_picker_prompt() -> str:
    """Read the gpt-4o-mini asset-picker prompt + append the JSON contract."""
    base = _PICKER_PROMPT_PATH.read_text(encoding="utf-8")
    return base + _JSON_OUTPUT_CONTRACT


def build_asset_picker_context() -> BaseContext:
    """Build the BaseContext for AssetPickerAgent.

    Single-shot vision task. No tools, no iteration, no dynamic context.
    """
    return BaseContext(
        doc_paths=[],
        static_prefix=_load_picker_prompt(),
    )
