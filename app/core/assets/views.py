"""AssetView — prepared form an LLM-message builder can consume.

`display` is the multimodal content block in Anthropic's shape (the
OpenAI provider translates Anthropic `image` blocks → Responses-API
`input_image` in `OpenAIProvider._convert_messages`). `display=None`
means the asset has no renderable preview at this stage; the model
still sees `text_caption` + filename metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RenderTarget(str, Enum):
    """Which provider's vision endpoint these views are being prepared for.

    Adapter selection + render-size defaults differ per target — Claude
    Sonnet 4.6 accepts up to 1568px long edge with high fidelity; OpenAI
    gpt-4o-mini in `detail: low` mode caps at 512².
    """
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


@dataclass(slots=True)
class AssetView:
    """Prepared asset bound to a specific render target."""
    # Original source bytes (used for upload / persistence, NOT for vision).
    raw_bytes: bytes
    # Original source MIME (svg+xml stays svg+xml here; rasterized payload
    # is inside `display`).
    raw_content_type: str
    # Multimodal content block in Anthropic shape, ready to pass to
    # `session.append_user_message`. None when the adapter couldn't render
    # (e.g. SvgAdapter Phase-1 stub).
    display: dict[str, Any] | None
    # Human-readable caption interleaved into the surrounding text message.
    text_caption: str
    # Adapter-resolved canonical format token: jpeg / png / gif / webp / svg.
    fmt: str
    # Free-form provenance string carried from `AssetRef.origin`.
    origin: str = ""
    # Caller-supplied metadata + adapter-added enrichment (e.g.
    # `background_hint` from Phase-2 luminance sniff).
    meta: dict = field(default_factory=dict)
