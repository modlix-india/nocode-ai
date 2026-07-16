"""System prompt for EssenceAnalyst - typed essence of competitor ad creatives.

Single-shot vision task, no tools. The enum lists in the prompt are generated
from the ``Essence`` Literals at import time, so the instruction and the
validator can never disagree. Output shape enforced the VisionAnalyst way:
fenced JSON in the final message + pydantic parse on the way out.
"""

from __future__ import annotations

from typing import get_args

from app.core.context import BaseContext

from app.agents.adzump.creative_intelligence.models import (
    AwarenessStage,
    CopyFramework,
    EmotionalAngle,
    HookType,
    MediaFormat,
    Offer,
    Proof,
    VisualStyle,
)


def _enum(literal) -> str:
    return " | ".join(get_args(literal))


_ESSENCE_PROMPT = f"""You are an advertising-creative analyst. You will be shown \
N competitor ad creatives (images; for video ads, the poster still), each with \
the ad copy that ran alongside it. For EACH image, in order, extract its \
ESSENCE - what the ad IS, what it is ABOUT, and how it is BUILT - as structured \
fields another system will use to generate similar ads for a different product.

Three layers per image:

STRATEGY - the reasoning a copywriter would reproduce:
- angle: the core promise in one product-agnostic phrase (e.g. "save time for \
busy parents"). Read it from the copy + image together.
- hook_type: {_enum(HookType)}.
- hook_text: the literal opening fragment of the ad copy (or the dominant \
on-image headline when there is no copy). Verbatim, <= 120 chars.
- awareness_stage: the Eugene Schwartz stage the ad targets: {_enum(AwarenessStage)}.
- copy_framework: {_enum(CopyFramework)} - 'none' unless the copy clearly follows one.
- emotional_angle: {_enum(EmotionalAngle)}.
- offer: {_enum(Offer)}.
- proof: {_enum(Proof)}.

WHAT IS IT:
- subject: what is physically shown (product / person / scene), one phrase.

VISUAL REFERENCE - what an image generator would reproduce:
- media_format: {_enum(MediaFormat)}.
- visual_style: {_enum(VisualStyle)}.
- layout: the composition in one phrase (e.g. "split screen, product left, \
text right").
- ocr_text: ALL text visible IN the image, verbatim. Empty if none.
- colors: 2-4 dominant colors, CSS names or hex.

Rules:
- Every enum carries an escape value (other / none / unknown) - use it when \
unsure. Do NOT force a wrong bucket and do NOT invent.
- ocr_text comes from the IMAGE only, never from the ad-copy metadata.
- Judge each image independently.

## Output contract

Your FINAL message MUST be a single fenced ```json block and nothing else - \
one verdict per input image, in input order:

```json
{{
  "verdicts": [
    {{"idx": 0, "angle": "own a home by the lake", "hook_type": "aspiration",
     "hook_text": "Lakeside living from 1.2Cr", "awareness_stage": "solution_aware",
     "copy_framework": "none", "emotional_angle": "status", "offer": "none",
     "proof": "none", "subject": "aerial shot of villas by a lake",
     "media_format": "static_image", "visual_style": "lifestyle",
     "layout": "full-bleed photo, headline bottom-left",
     "ocr_text": "LAKESIDE VILLAS | Book a visit", "colors": ["teal", "white"]}}
  ]
}}
```

Emit a verdict for EVERY image. snake_case keys exactly as above; enum values \
exactly from the lists."""


def build_essence_context() -> BaseContext:
    """BaseContext for the essence pass. No docs, no dynamic context."""
    return BaseContext(doc_paths=[], static_prefix=_ESSENCE_PROMPT)
