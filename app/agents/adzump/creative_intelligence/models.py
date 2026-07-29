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


# Essence enums - every one carries an escape value (other/none/unknown) so the
# vision pass is never forced into a wrong bucket; the long-tail free-str fields
# (angle, hook_text, layout, subject) cover nuance the enums can't.
HookType = Literal[
    "social_proof", "urgency", "curiosity", "pain_point", "aspiration",
    "offer", "demonstration", "question", "bold_claim", "other",
]
AwarenessStage = Literal[  # Eugene Schwartz
    "unaware", "problem_aware", "solution_aware", "product_aware", "most_aware", "unknown",
]
CopyFramework = Literal["PAS", "AIDA", "BAB", "FAB", "star_story_solution", "none"]
EmotionalAngle = Literal[
    "fear", "greed", "desire", "belonging", "status", "relief", "trust",
    "excitement", "urgency", "other",
]
Offer = Literal["discount", "bundle", "free_trial", "guarantee", "bogo", "free_shipping", "none"]
Proof = Literal["before_after", "testimonial", "lab_data", "demonstration", "ratings", "ugc", "none"]
MediaFormat = Literal[
    "static_image", "video", "carousel", "ugc", "testimonial", "founder_story",
    "listicle", "skit", "comparison", "product_demo", "other",
]
VisualStyle = Literal[
    "minimalist", "lifestyle", "studio", "ugc_authentic", "text_heavy", "animated", "meme", "other",
]
# Deterministic run-time proxy for "how proven is this creative" (NOT a metric,
# NOT a vision field) - computed on Creative from longevity + activity.
WinnerSignal = Literal["testing", "promising", "winner", "evergreen"]


class Essence(BaseModel):
    """What a competitor creative IS, is ABOUT, and is BUILT like - DERIVED by the
    vision essence worker (one multimodal pass), not from the vendor. This is the
    reasoning a generation agent slots into a prompt and the image agent uses as a
    reference. Three layers: strategy (why it works), what-is-it, visual-reference.
    All defaulted: absent until the worker has run. ``winner_signal`` (how proven)
    is NOT here - it's a deterministic computed_field on ``Creative``."""

    model_config = ConfigDict(populate_by_name=True)

    # ── Strategy: the reasoning a copy/creative generator reproduces ──
    angle: str = ""                                              # core promise, product-agnostic (free str)
    hook_type: HookType = Field(default="other", alias="hookType")
    hook_text: str = Field(default="", alias="hookText")         # literal opening copy fragment
    awareness_stage: AwarenessStage = Field(default="unknown", alias="awarenessStage")
    copy_framework: CopyFramework = Field(default="none", alias="copyFramework")
    emotional_angle: EmotionalAngle = Field(default="other", alias="emotionalAngle")
    offer: Offer = "none"
    proof: Proof = "none"

    # ── What is it ──
    subject: str = ""                                            # product / person / scene shown

    # ── Visual reference: what the image agent reproduces ──
    media_format: MediaFormat = Field(default="other", alias="mediaFormat")
    visual_style: VisualStyle = Field(default="other", alias="visualStyle")
    layout: str = ""                                             # composition (free str)
    ocr_text: str = Field(default="", alias="ocrText")           # verbatim on-image text
    colors: list[str] = Field(default_factory=list)              # dominant palette


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
    # Perceptual (DCT) hash of the rehosted image - Tier-2 near-duplicate key
    # (see dedup.py). Empty when the bytes weren't a decodable raster image.
    perceptual_hash: str = Field(default="", alias="perceptualHash")

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
    essence: Essence | None = None

    @computed_field(alias="winnerSignal")
    @property
    def winner_signal(self) -> WinnerSignal:
        """Deterministic 'how proven is this creative' proxy - NO model call, and
        not a metric it doesn't have. Longevity is the signal: advertisers kill
        losers fast, so a long-running ad is a proven one. ``days_running`` is
        adlibrary's ``days_count`` (confirm it means days-active, not days-since-
        first-seen, before fully trusting the thresholds). ``is_active`` + the
        metrics bag are available to refine this later."""
        days = self.days_running or 0
        if days >= 90:
            return "evergreen"
        if days >= 60:
            return "winner"
        if days >= 30:
            return "promising"
        return "testing"


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
