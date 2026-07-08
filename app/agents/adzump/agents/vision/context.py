"""System prompt for VisionAnalyst.

The prompt lives in ``agents/product/prompts/product_assets.txt`` today.
The agent reads it from there to preserve byte-for-byte parity with the
existing direct-call shape. A v2 refactor can move the file under
``agents/vision/prompts/``.

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
_SELECT_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "product" / "prompts" / "product_assets.txt"
)


# Output-contract suffix. The legacy direct call got this for free from
# OpenAI's ``response_format`` (schema-enforced); without that, the model needs an
# explicit JSON shape instruction. Keep it short - the heavy prompt-
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
      "role": "developer",            // "developer" | "project" | "cobrand" | "main" - short label, may be ""
      "reasoning": "one sentence",
      "background_hint": "light"      // "light" | "dark" | "" - UI tile contrast from the thumbnail
    }
  ],
  "creatives": [
    {
      "idx": 3,                       // index into the candidate list
      "role": "hero",                 // "hero" | "amenity" | "floor_plan" | "unused" - MUST be one of these four exactly
      "reasoning": "main exterior shot" // <= 120 chars
    },
    {"idx": 7, "role": "amenity", "reasoning": "pool"},
    {"idx": 12, "role": "floor_plan", "reasoning": "2BHK unit plan"}
  ],
  "confidence": 0.85,                 // 0.0..1.0 self-assessed precision on logos
  "note": "string or empty"           // ONE sentence if logos=[] OR a logo-looking candidate was rejected
}
```

Hard caps: at most 3 logos. Use empty list/string/0 when there's no signal - do not invent.
**Always emit `creatives` (the role-tagged list), NOT a flat `creative_idxs` array** - the older shape is deprecated and produces empty roles downstream.
"""


def _load_select_prompt() -> str:
    """Read the gpt-4o-mini select prompt + append the JSON contract."""
    base = _SELECT_PROMPT_PATH.read_text(encoding="utf-8")
    return base + _JSON_OUTPUT_CONTRACT


def build_select_context() -> BaseContext:
    """Build the BaseContext for the select-subset (scrape) mode.

    Single-shot vision task. No tools, no iteration, no dynamic context.
    """
    return BaseContext(
        doc_paths=[],
        static_prefix=_load_select_prompt(),
    )


# ── review-each mode (upload path) ───────────────────────────────────────────
# Select-subset PICKS from scraped candidates; review-each returns a verdict per
# image. Different task → different prompt. The key behavior: never guess when
# unsure - flag needs_user and ask.
_REVIEW_PROMPT = """You are a vision reviewer for advertising assets. You will be \
shown N images (pasted by a user for their product/service). For EACH image, in \
order, decide:
- role: 'logo' | 'hero' | 'amenity' | 'floor_plan' | 'unused' | 'unknown'.
- relevant: is this usable for the product's ads at all? (a meme, a screenshot, \
an unrelated stock photo → relevant=false).
- confidence: 0.0..1.0 in your own verdict.
- needs_user: if you are NOT confident what the image is or whether to use it, \
set true and write a short question for the user. Do NOT guess - asking is \
better than a wrong silent choice.

You CANNOT confirm from an image alone that it belongs to the user's specific \
project (two real-estate projects look alike) - do NOT try, and do NOT reject a \
plausible asset just because you can't verify the project. BUT if an image \
clearly shows a DIFFERENT brand or project than the brief (a competitor's name \
or logo, or content that contradicts the product), set needs_user=true and ask \
the user to confirm it's their own. The user's note, if given, is their claim of \
ownership - weigh it.

Review every image independently. Do not select a subset; emit one verdict per \
image."""

_REVIEW_JSON_CONTRACT = """

## Output contract

Your FINAL message MUST be a single fenced ```json block and nothing else, with \
exactly one verdict per input image, in input order:

```json
{
  "verdicts": [
    {"idx": 0, "role": "logo", "relevant": true, "confidence": 0.95, "needs_user": false, "question": "", "reasoning": "clean brand wordmark"},
    {"idx": 1, "role": "unknown", "relevant": true, "confidence": 0.4, "needs_user": true, "question": "Is image 2 a floor plan or a site map?", "reasoning": "ambiguous line drawing"}
  ]
}
```

Emit a verdict for every image. Use empty string / 0.0 / false when there's no signal - do not invent."""


def build_review_context() -> BaseContext:
    """BaseContext for review-each (upload) mode. Same single-shot engine,
    different instruction + output shape than select-subset."""
    return BaseContext(
        doc_paths=[],
        static_prefix=_REVIEW_PROMPT + _REVIEW_JSON_CONTRACT,
    )
