"""The one shape for a market creative and a competitor.

A single typed model carries a creative from the vendor adapter, through the
store, to the craft renderer. There is no second grouped-dict vocabulary and no
flatten step: an adapter emits ``Creative`` objects, the store persists
``model_dump(by_alias=True)`` (the camelCase DB record), and a read validates the
stored dict straight back into the model. Aliases match the field names already
in storage, so existing records parse with no migration.

``totalCreatives`` / ``activeCreatives`` are computed from ``creatives`` so the
counts can never drift from the list - there is no separate stats copy to keep in
sync.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

MediaType = Literal["image", "video", "carousel", "collection"]
FetchStatus = Literal["ok", "empty", "error"]

SCHEMA_VERSION = 1
SOURCE = "adlibrary.com"
# Ceiling on creatives stored per competitor so one prolific advertiser can't
# blow the Mongo document size limit; also the per-competitor fetch cap.
MAX_CREATIVES_PER_COMPETITOR = 60


class Insights(BaseModel):
    """The essence of a creative - DERIVED by us (vision over the stored image +
    the metrics we already fetched), not from the vendor. This is the reasoning
    the creative agent builds on and the image agent references when generating a
    new creative. All optional: absent until the essence worker has run.

    The four load-bearing fields answer what the creative *is*, what it's *about*,
    how it's *built*, and how it's *doing*. ``ocr_text`` and ``colors`` are the
    raw extraction outputs ``how_its_made`` summarizes - kept because the image
    agent wants the literal on-image text and palette."""

    model_config = ConfigDict(populate_by_name=True)

    what_it_is: str = Field(default="", alias="whatItIs")      # subject, product shown, offer/claim
    theme: str = ""                                            # angle, mood, message
    how_its_made: str = Field(default="", alias="howItsMade")  # format, composition, text-on-image, hook, CTA
    performance: str = ""                                      # read of the metrics (spend/impressions/active-days)
    ocr_text: str = Field(default="", alias="ocrText")
    colors: list[str] = Field(default_factory=list)


class Creative(BaseModel):
    """One competitor ad creative. Vendor-agnostic: an adapter maps its raw
    payload onto these fields, so nothing downstream knows which source it came
    from. For videos ``source_asset_url`` is the video and ``poster_*`` the still;
    for images the poster fields stay empty."""

    model_config = ConfigDict(populate_by_name=True)

    creative_id: str = Field(default="", alias="creativeId")
    media_type: MediaType = Field(default="image", alias="mediaType")

    # Binary: rehosted into our Files store (fileUrl / posterUrl) vs the vendor's
    # own (undocumented-TTL) URLs we fall back to until a rehost lands.
    file_url: str = Field(default="", alias="fileUrl")
    source_asset_url: str = Field(default="", alias="sourceAssetUrl")
    poster_url: str = Field(default="", alias="posterUrl")
    poster_source_url: str = Field(default="", alias="posterSourceUrl")
    content_hash: str = Field(default="", alias="contentHash")

    headline: str = ""
    primary_text: str = Field(default="", alias="primaryText")
    description: str = ""
    cta: str = ""
    landing_url: str = Field(default="", alias="landingUrl")

    platform: str = ""
    format: str = ""
    publisher_platforms: list[str] = Field(default_factory=list, alias="publisherPlatforms")

    first_seen: str = Field(default="", alias="firstSeen")
    last_seen: str = Field(default="", alias="lastSeen")
    is_active: bool = Field(default=False, alias="isActive")
    days_running: int = Field(default=0, alias="daysRunning")
    variants: int = 0

    # Vendor-variable numeric bag (impressions/likes/spend/…); kept as a dict
    # because which keys a source exposes differs per vendor.
    metrics: dict[str, Any] = Field(default_factory=dict)
    insights: Insights | None = None


class Competitor(BaseModel):
    """A competitor's creative record as stored in the library, keyed by
    ``competitor_key`` (normalized host). The renderable image URL for each
    creative is chosen by ``Creative`` field precedence, not by a second stats
    structure."""

    model_config = ConfigDict(populate_by_name=True)

    competitor_key: str = Field(default="", alias="competitorKey")
    name: str = ""
    aliases: list[str] = Field(default_factory=list)
    domain: str = ""
    platform_ids: dict[str, Any] = Field(default_factory=dict, alias="platformIds")
    business_type: str = Field(default="", alias="businessType")
    location: str = ""
    pricing: str = ""
    logo_url: str = Field(default="", alias="logoUrl")

    creatives: list[Creative] = Field(default_factory=list)

    source: str = SOURCE
    last_fetched_at: str = Field(default="", alias="lastFetchedAt")
    fetch_status: FetchStatus = Field(default="ok", alias="fetchStatus")
    schema_version: int = Field(default=SCHEMA_VERSION, alias="schemaVersion")

    @computed_field(alias="totalCreatives")
    @property
    def total_creatives(self) -> int:
        return len(self.creatives)

    @computed_field(alias="activeCreatives")
    @property
    def active_creatives(self) -> int:
        return sum(1 for c in self.creatives if c.is_active)
