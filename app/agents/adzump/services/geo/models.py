"""Typed geo-targeting location models.

Locations travel the pipeline as plain dicts — they arrive as LLM tool params
and leave as JSON in storage — so these models are *validate-at-the-boundary*:
``PlatformGeoMapper`` builds one at the end of mapping, then dumps it back to a
dict so nothing downstream has to change. The point is to make each platform's
contract explicit and enforced at the one place locations are produced:

  * A ``MetaGeoLocation`` with no ``meta_type`` cannot be constructed — Meta adset
    creation buckets every target by type (zips/cities/regions/countries), so a
    typeless location is unusable. That missing invariant caused the original bug.
  * A ``GoogleGeoLocation``'s ``google_id`` is normalized to the
    ``geoTargetConstants/{id}`` resource-name form Google Ads expects.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Meta adgeolocation `type` values we currently resolve to. Not enforced as an
# enum on the model — Meta may return finer-grained types (subcity, neighborhood,
# …) and rejecting those would lose a location, which is worse than the bug we're
# fixing. Kept here as the documented set the mapper produces.
META_LOCATION_TYPES = frozenset(
    {"zip", "city", "region", "country", "geo_market", "neighborhood"}
)


class GeoLocationBase(BaseModel):
    """Platform-agnostic target area — the 'where' produced by discovery."""

    # extra="allow": a location accretes transient/optional keys across the
    # pipeline (and survives platform-switch round-trips). Preserve anything we
    # don't model rather than silently dropping it on a model round-trip.
    model_config = ConfigDict(extra="allow")

    name: str = ""
    city: str = ""
    state: str = ""
    pincode: str = ""
    lat: float | None = None
    lng: float | None = None
    distance_km: float = 0.0
    place_id: str | None = None
    # "city" | "state" | "country" — set only for broad (non-local) campaigns.
    geo_level: str = ""


class GoogleGeoLocation(GeoLocationBase):
    """Target area resolved to a Google Ads geo target constant."""

    google_id: str | None = None
    google_name: str | None = None

    @field_validator("google_id")
    @classmethod
    def _as_resource_name(cls, v: str | None) -> str | None:
        """Google Ads targets a geo by its ``geoTargetConstants/{id}`` resource name."""
        if not v:
            return v
        v = str(v)
        return v if v.startswith("geoTargetConstants/") else f"geoTargetConstants/{v}"


class MetaGeoLocation(GeoLocationBase):
    """Target area resolved to a Meta Ads geolocation.

    ``meta_type`` is required and non-empty — it is the bucket key Meta adset
    creation sorts each target into, so it must always be present even when the
    key lookup found nothing (the lat/lng radial fallback still needs the type).
    """

    meta_type: str = Field(min_length=1)
    meta_key: str | None = None
    meta_name: str | None = None
