"""The port every ad-intelligence vendor plugs into.

The library and store depend on this protocol, never on a concrete vendor - so
adding Meta's ad library or TikTok's creative center is a new adapter file, not a
change to anything downstream. An adapter's whole job is: query the vendor, map
its raw payload onto ``Creative`` objects, and report the competitor identity it
resolved.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.agents.adzump.creative_intelligence.models import Creative


class SourceFetch(BaseModel):
    """What a source returns for one competitor: the creatives plus the identity
    fields it could resolve from the batch (logo, cleaner name, platform ids).
    Identity fields default empty when the vendor doesn't expose them."""

    creatives: list[Creative] = Field(default_factory=list)
    resolved_name: str = ""
    logo_url: str = ""
    platform_ids: dict[str, Any] = Field(default_factory=dict)


class AdIntelligenceSource(Protocol):
    """A vendor of competitor ad creatives. Query by brand ``name`` (the one mode
    every source supports); ``domain`` narrows/dedupes when the vendor exposes an
    advertiser domain. Raises on non-recoverable failures (auth/credits) so the
    library can serve stale rather than store an empty record."""

    async def fetch(self, *, domain: str, name: str) -> SourceFetch: ...
