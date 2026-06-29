"""Typed geo-targeting location models.

A target area is the platform-agnostic "where" (name, coordinates, scale). Each
ad platform then resolves it to its own targeting handle, kept in a *platform-only*
sub-model so the two concerns never bleed into one another:

  * ``MetaGeoLocation`` — Meta's native shape ``{type, key, name}``. ``type`` is
    required and non-empty: Meta adset creation buckets every target by type
    (zips/cities/regions/countries), so a typeless location is unusable. That
    missing invariant caused the original bug.
  * ``GoogleGeoLocation`` — ``{id, name}``; ``id`` is normalized to the
    ``geoTargetConstants/{id}`` resource name Google Ads expects.

``TargetArea`` composes them — ``area.meta`` / ``area.google`` is populated for
the active platform — so a mapped location carries the scale once (``scale``) and
the platform handle once (``meta.type``), never a duplicated ``meta_type`` +
``geo_level`` pair.

Locations still travel the pipeline as dicts (LLM tool params in, JSON storage
out), so ``PlatformGeoMapper`` builds a ``TargetArea`` at the end of mapping and
``model_dump()``s it straight back to a (now nested) dict.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class MetaGeoLocation(BaseModel):
    """Meta Ads targeting handle — Meta's native adgeolocation shape.

    ``type``/``key``/``name`` are Meta's own field names (a ``geo_locations``
    entry is ``{key, name, …}`` bucketed by ``type``)."""

    # Required & non-empty (that's the invariant). Typically zip|city|region|
    # country, but left as a free string, not an enum: Meta can return finer types
    # (subcity, neighborhood, …) and rejecting those would lose a location.
    type: str = Field(min_length=1)
    key: str | None = None
    name: str | None = None


class GoogleGeoLocation(BaseModel):
    """Google Ads targeting handle — a geo target constant.

    Uses Google's own field name: the identifier is the constant's
    ``resourceName`` (``geoTargetConstants/{id}``), not the bare numeric ``id``."""

    resourceName: str | None = None
    name: str | None = None

    @field_validator("resourceName")
    @classmethod
    def _as_resource_name(cls, v: str | None) -> str | None:
        """Normalize to the ``geoTargetConstants/{id}`` resource-name form."""
        if not v:
            return v
        v = str(v)
        return v if v.startswith("geoTargetConstants/") else f"geoTargetConstants/{v}"


class TargetArea(BaseModel):
    """A campaign target area: the generic 'where' plus the active platform handle."""

    name: str = ""
    city: str = ""
    state: str = ""
    pincode: str = ""
    lat: float | None = None
    lng: float | None = None
    distance_km: float = 0.0
    place_id: str | None = None
    # "city" | "state" | "country" — set only for broad (non-local) campaigns.
    scale: str | None = None
    meta: MetaGeoLocation | None = None
    google: GoogleGeoLocation | None = None
