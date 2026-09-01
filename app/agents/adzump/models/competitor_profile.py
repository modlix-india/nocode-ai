"""Typed competitor entry - the contract for
``session_context["competitor_analysis"]["competitors"]``.

Entries are born as Product Analyst JSON (schema in agents/product/context.py),
persisted verbatim in the durable business record, and mutated once at runtime
when fetch_competitor_creatives attaches creatives. The session keeps plain
dicts for JSON persistence; every reader and writer goes through this model.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# Optional-by-schema fields whose explicit null must survive the parse; every
# other null is an LLM artifact and falls back to the field default.
_NULLABLE_FIELDS = {"url", "pricing", "weakness"}


class CompetitorProfile(BaseModel):
    """One competitor as discovered by analysis, enriched by the creative fetch."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    name: str = ""
    url: str | None = None
    business_type: str = ""
    location: str = ""
    pricing: str | None = None
    key_usps: list[str] = Field(default_factory=list)
    weakness: str | None = None
    why_competitor: str = ""
    # Attached by fetch_competitor_creatives. None = never fetched (badge-less
    # card); [] = fetched and the ad library had none ("No ads found").
    creatives: list[dict] | None = None
    total_creatives: int = Field(0, alias="totalCreatives")
    active_creatives: int = Field(0, alias="activeCreatives")

    @classmethod
    def from_stored(cls, raw: dict) -> "CompetitorProfile":
        """Lenient parse of a stored or LLM-emitted entry.

        Absorbs the legacy shape where a business-shaped dict carries
        ``product_name`` instead of ``name``. Never raises on a malformed
        entry - the name survives, the rest falls back to defaults."""
        data = dict(raw or {})
        if not data.get("name") and data.get("product_name"):
            data["name"] = data.pop("product_name")
        data = {
            k: v for k, v in data.items()
            if v is not None or k in _NULLABLE_FIELDS
        }
        try:
            return cls.model_validate(data)
        except Exception as e:
            logger.warning("competitor_profile: malformed entry %r: %s",
                           data.get("name"), str(e)[:200])
            return cls(name=str(data.get("name") or ""))

    def to_stored(self) -> dict:
        """By-alias dump matching the stored shape. Omits the creatives triad
        entirely when never fetched - key absence is how the craft card tells
        "unfetched" from "fetched, zero ads"."""
        data = self.model_dump(by_alias=True)
        if self.creatives is None:
            data.pop("creatives", None)
            data.pop("totalCreatives", None)
            data.pop("activeCreatives", None)
        return data


def competitor_profiles(session_ctx: dict) -> list[CompetitorProfile]:
    """Typed read of the session's competitor entries."""
    competitive = session_ctx.get("competitor_analysis") or {}
    return [
        CompetitorProfile.from_stored(c)
        for c in competitive.get("competitors") or []
        if isinstance(c, dict)
    ]
