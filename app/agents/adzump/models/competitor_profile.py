"""Typed competitor entry - the contract for
``session_context["competitor_analysis"]["competitors"]``.

Entries are born as Product Analyst JSON (schema in agents/product/context.py)
and mutated once at runtime when fetch_competitor_creatives attaches creatives.
Their home is the adzump_competitors table: a resume loads them from it and
every save writes the chat's list back (product_service). The session keeps
plain dicts for JSON persistence; every reader and writer goes through this model.
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

    # adzump_competitors row id once saved: an entry whose row is gone was
    # deleted elsewhere (the library UI), so the next save drops it. Not
    # `competitor_id` - that is the analyst's evidence citation ("C3").
    row_id: int | None = None
    name: str = ""
    url: str | None = None
    url_source: str = ""  # "user" = pinned by the user; research never overrides it
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
