"""Wire-shape models for the AssetPickerAgent's LLM output.

These mirror the private ``_LogoChoice`` / ``_AssetSelection`` classes that
lived inside ``agents/product/product_assets.py``. Moved here so the agent
owns its own contract — and so the JSON shape and the Pydantic validator
share a single source of truth.

The agent's public output is still ``ProductAssets`` (with ``LogoPick``
entries), imported from ``agents.product.models`` for now. See D4 in the
implementation notes for the future move plan.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class LogoChoice(BaseModel):
    """One brand-logo selection in the LLM's JSON output.

    Carries the candidate ``idx``, not the URL — the agent's post-loop
    resolver maps idx → URL and builds the public ``LogoPick``. Don't
    confuse the two: this is the wire shape the model fills.
    """
    idx: int = Field(description="Index into the candidate list.")
    role: str = Field(
        default="",
        description="Short label: 'developer', 'project', 'cobrand', or 'main'.",
    )
    reasoning: str = Field(
        default="",
        description="One short sentence — why this image is the named brand's logo.",
    )
    background_hint: str = Field(
        default="",
        description="UI tile contrast hint: 'dark' (light logo), 'light' (dark logo), '' (no hint).",
    )


class CreativeChoice(BaseModel):
    """One creative-image role assignment in the LLM's JSON output.

    Shift 2 addition (post-v7 redesign · 2026-05-21). Mirrors ``LogoChoice``
    shape so the picker has a consistent contract: model emits per-candidate
    role, code derives the aggregate ``creative_completeness`` shape.

    Roles are intentionally a tight enum (3 ad-usable categories plus
    ``unused``). Per Kiran's Q3 pick: "code gathers, model decides" —
    model labels each candidate visually, code counts.
    """
    idx: int = Field(description="Index into the candidate list.")
    role: str = Field(
        description=(
            "One of: 'hero', 'amenity', 'floor_plan', 'unused'. "
            "Hero = the main building/exterior/banner shot. "
            "Amenity = pool, gym, lounge, gardens, lifestyle. "
            "Floor_plan = unit plans or masterplans. "
            "Unused = candidate is real content but not a useful ad creative."
        ),
    )
    reasoning: str = Field(
        default="",
        description="<= 120 chars on why this role.",
    )


class AssetSelection(BaseModel):
    """The shape the LLM fills. Indices into the candidate list."""
    logos: list[LogoChoice] = Field(
        default_factory=list,
        max_length=3,
        description="Brand logos visible in the candidates. 0..3 picks.",
    )
    creatives: list[CreativeChoice] = Field(
        default_factory=list,
        description=(
            "Shift 2: per-candidate role assignment for ad creatives. "
            "One entry per candidate the model considers a usable creative — "
            "ranked best first. Code derives the aggregate creative_completeness "
            "(hero_found, amenities_count, floor_plan_found, verdict, missing_categories)."
        ),
    )
    creative_idxs: list[int] = Field(
        default_factory=list,
        description=(
            "[Deprecated post-Shift-2 · kept for back-compat with v6/v7 logs.] "
            "Indices of product/service photographs, ranked best first. "
            "Use `creatives` field for new code — derive idxs from it."
        ),
    )
    confidence: float = Field(
        default=0.0,
        description="Self-assessed precision on the logo picks, 0.0 to 1.0.",
    )
    note: str = Field(
        default="",
        description="One sentence when logos=[] or a logo-looking candidate was rejected.",
    )
