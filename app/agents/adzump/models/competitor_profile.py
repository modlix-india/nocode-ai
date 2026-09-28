"""Typed competitor entry - the contract for
``session_context["competitor_analysis"]["competitors"]``.

Entries are born as Product Analyst JSON (schema in agents/product/context.py)
and mutated once at runtime when fetch_competitor_creatives attaches a preview
of the latest ads (the full ads live in adzump_creatives).
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
# The ads a chat carries per competitor: the panel shows at most these, and a
# full ad (essence, copy, renditions) stays in adzump_creatives - carried in
# the context they sank every save past its 64KB column (live 2026-09-25).
CHAT_ADS_PER_COMPETITOR = 10


class AdPreview(BaseModel):
    """What the chat keeps of one competitor ad: exactly what the panel draws."""

    model_config = ConfigDict(populate_by_name=True)

    creative_id: str = Field("", alias="creativeId")
    media_type: str = Field("image", alias="mediaType")
    file_url: str = Field("", alias="fileUrl")
    poster_url: str = Field("", alias="posterUrl")
    headline: str = ""
    is_active: bool = Field(False, alias="isActive")
    last_seen: str = Field("", alias="lastSeen")
    days_running: int = Field(0, alias="daysRunning")


def ad_previews(creatives: list[dict]) -> list[dict]:
    """The chat's view of a competitor's ads (stored-shape dicts): previews of
    the latest CHAT_ADS_PER_COMPETITOR - active first, then newest."""
    latest = sorted(creatives, reverse=True,
                    key=lambda c: (bool(c.get("isActive")), c.get("firstSeen") or ""))
    return [AdPreview.model_validate(c).model_dump(by_alias=True)
            for c in latest[:CHAT_ADS_PER_COMPETITOR]]


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
    # Attached by fetch_competitor_creatives as ad_previews(). None = never
    # fetched (badge-less card); [] = fetched and none kept ("No ads found").
    # The totals count every stored ad, not just the previews.
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
