"""Product - typed schema for ``session_ctx["product_data"]``.

The written-down schema of adzump's central shared state (previously
discoverable only by grep). The live session object stays a plain dict -
mutated by reference across sub-sessions, serialized at many boundaries -
so adoption is incremental: ``check_product`` warns at the save/restore
boundary, tests enforce strictly (test_product_model.py).

``Place`` lives in the sibling ``place`` leaf so downstream builders can
import it without dragging in this schema.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents.adzump.models.place import Place
from app.agents.adzump.agents.location.models import TargetArea
from app.agents.adzump.agents.product.models import SiteLink

logger = logging.getLogger(__name__)


class Contact(BaseModel):
    """How to reach the business - scraped from the site."""

    phone: str = ""
    email: str = ""


class Page(BaseModel):
    """Scrape state for one fetched page - keyed by URL in ``Product.pages``.
    Entry presence = the scrape succeeded (drives dedupe + the scrape budget)."""

    screenshot_url: str = ""  # full-page capture upload


class Logo(BaseModel):
    """One logo candidate - ``Assets.logos[0]`` is the primary."""

    url: str
    display: dict[str, Any] = Field(default_factory=dict)  # render hints (background, fit)
    source: str = ""  # "user_upload" | the vision pick's source
    source_url: str = ""
    role: str = ""  # main | developer | project
    reasoning: str = ""  # vision LLM's one-line pick rationale
    format: str = ""
    confidence: float = 0.0  # arbitration: a new batch only replaces a higher one; uploads pin 1.0


class Image(BaseModel):
    """One product image - picked from the site or uploaded by the user."""

    url: str
    display: dict[str, Any] = Field(default_factory=dict)  # render hints (fit, thumb_url)
    role: str = ""  # hero | amenity | floor_plan
    source: str = ""  # "site_pick" | "user_upload"


class Assets(BaseModel):
    """Raw material for creative generation - everything fetched from the
    site or uploaded by the user. Generated creatives are campaign output,
    not assets; they live on the campaign object."""

    logos: list[Logo] = Field(default_factory=list)
    images: list[Image] = Field(default_factory=list)


class Product(BaseModel):
    """``extra="allow"`` - unknown keys never reject a live dict; they surface
    via ``model_extra`` (warned at runtime, failed in tests)."""

    model_config = ConfigDict(extra="allow")

    # ── Profile - written by the product agent ──
    product_name: str = ""
    business_type: str = ""
    business_scale: Literal["local", "regional", "national", "international"] = "national"  # picks the geo tool
    summary: str = ""
    place: Place = Field(default_factory=Place)  # where the business IS; ads go to target_areas
    pricing: str = ""
    contact: Contact = Field(default_factory=Contact)
    unique_features: list[str] = Field(default_factory=list)
    products_services: list[str] = Field(default_factory=list)

    # ── Scrape state ──
    primary_url: str = ""
    # In scrape order; the primary screenshot is derived (_shared.primary_screenshot_url)
    pages: dict[str, Page] = Field(default_factory=dict)
    pages_analyzed: list[str] = Field(default_factory=list)  # the LLM's claim of pages it used
    site_links: list[SiteLink] = Field(default_factory=list)

    # ── Assets - creative-gen raw material; only logos[0] + images persist ──
    assets: Assets = Field(default_factory=Assets)

    # ── Geo targeting - written by finalize_targets ──
    # Platform handle nested per-area; "mapped" = handle presence (platform.is_mapped_for)
    target_areas: list[TargetArea] = Field(default_factory=list)


def check_product(product_data: dict, where: str) -> None:
    """Warn-only schema check at the save/restore boundary. Never raises -
    drift must not kill a live session; the strict check lives in tests."""
    try:
        product = Product.model_validate(product_data)
    except ValidationError as e:
        first = e.errors()[0]
        logger.warning(
            "product_schema_drift[%s]: %s - %s",
            where, ".".join(str(p) for p in first["loc"]), first["msg"],
        )
        return
    if product.model_extra:
        logger.warning(
            "product_schema_unknown_keys[%s]: %s", where, sorted(product.model_extra)
        )
