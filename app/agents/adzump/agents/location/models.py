"""Typed geo-targeting location models - the location agent's data vocabulary.

A target area is the platform-agnostic "where" (name, coordinates, scale). Each
ad platform then resolves it to its own targeting handle, kept in a *platform-only*
sub-model so the two concerns never bleed into one another:

  * ``MetaGeoLocation`` - Meta's native shape ``{type, key, name}``. ``type`` is
    required and non-empty: Meta adset creation buckets every target by type
    (zips/cities/regions/countries), so a typeless location is unusable. That
    missing invariant caused the original bug.
  * ``GoogleGeoLocation`` - ``{resourceName, name}``; the identifier is the
    geo-target-constant resource name (``geoTargetConstants/{id}``), Google's
    own field name for it - not the bare numeric ``id``.

``TargetArea`` composes them - ``area.meta`` / ``area.google`` is populated for
the active platform - so a mapped location carries the scale once (``scale``) and
the platform handle once (``meta.type``), never a duplicated ``type`` +
``geo_level`` pair.

Locations still travel the pipeline as dicts (LLM tool params in, JSON storage
out), so ``PlatformGeoMapper`` builds a ``TargetArea`` at the end of mapping and
``model_dump()``s it straight back to a (nested) dict.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class MetaGeoLocation(BaseModel):
    """Meta Ads targeting handle - Meta's native adgeolocation shape.

    ``type``/``key``/``name`` are Meta's own field names (a ``geo_locations``
    entry is ``{key, name, …}`` bucketed by ``type``)."""

    # Required & non-empty (that's the invariant). Typically zip|city|region|
    # country, but left as a free string, not an enum: Meta can return finer types
    # (subcity, neighborhood, …) and rejecting those would lose a location.
    type: str = Field(min_length=1)
    key: str | None = None
    name: str | None = None


class GoogleGeoLocation(BaseModel):
    """Google Ads targeting handle - a geo target constant.

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
    # "city" | "state" | "country" - set only for broad (non-local) campaigns.
    scale: str | None = None
    meta: MetaGeoLocation | None = None
    google: GoogleGeoLocation | None = None


# ── Edit-tool params - the manual-edit tools' input contract ─────────────────
#
# One model per edit tool. The tool schemas the LLM sees are GENERATED from
# these (``tool_params_from_model``), and each execute parses its raw params
# back into the model at the boundary - so required fields, defaults, and
# bounds live HERE, not as ad-hoc guards inside the executes.


class AddLocation(BaseModel):
    """Params for ``add_location`` - the area to append to targeting.

    ``radius`` is the wire name the model emits (km); ``add_location``
    stores it as ``TargetArea.distance_km``. Deliberately NO platform-handle
    fields (``resourceName``/``key``/``type``/``place_id``): anything in this
    schema is LLM-writable, and a hallucinated handle would skip the mapper's
    lookup - the one step that validates it. Handles are ALWAYS re-derived by
    ``PlatformGeoMapper`` from name/city/pincode; a future search-widget caller
    that already holds a handle gets its own structured (non-LLM) entry point."""

    name: str = Field(min_length=1, description="Display name of the area, e.g. 'Juhu'.")
    city: str = Field("", description="City the area belongs to, if the user gave one.")
    state: str = Field("", description="State/region, if the user gave one.")
    pincode: str = Field("", description="Postal code, if the user gave one.")
    lat: float | None = Field(None, description="Latitude, only if the user gave coordinates.")
    lng: float | None = Field(None, description="Longitude, only if the user gave coordinates.")
    radius: float = Field(5.0, description="Targeting radius in km around the area.")
    scale: str | None = Field(
        None,
        description=(
            "'city' | 'state' | 'country' when the area is that broad "
            "(e.g. 'add Mumbai' is a city). Omit for neighbourhoods/localities."
        ),
    )


class DeleteLocation(BaseModel):
    """Params for ``delete_location`` - which area to remove (1-based)."""

    index: int = Field(ge=1, description="1-based position in the current targeting list.")


def is_local_business(business_scale: str) -> bool:
    """Check if the resolved scale is local (radial-scan targeting applies)."""
    return (business_scale or "").strip().lower() == "local"
