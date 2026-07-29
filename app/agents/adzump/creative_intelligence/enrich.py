"""The enrichment seam - domain-side types for the injected essence worker.

The library's ingest accepts an optional ``enrich`` hook that maps rehosted
creative images to typed essence (Tier-3 of the cascade: vision adds, never
culls - see ``dedup.py``). The domain defines the SEAM but never constructs an
implementation; the tool injects one (the ``creative_essence`` sub-agent), so
``creative_intelligence`` stays model-free and the dependency points down.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from app.agents.adzump.creative_intelligence.models import Creative, Essence


class CreativeImage(BaseModel):
    """One deduped survivor to analyze: the stored creative + its rehosted
    image bytes (the same bytes the rehost hashed, so content_hash agrees)."""

    creative: Creative
    data: bytes
    content_type: str = "image/jpeg"


class EnrichCreatives(Protocol):
    """Maps creative images to ``{content_hash: Essence}``. Implementations may
    return a subset (a verdict that never parses is simply absent) and must not
    raise for a per-image failure - the library treats absence as essence=None."""

    async def __call__(self, images: list[CreativeImage]) -> dict[str, Essence]: ...
