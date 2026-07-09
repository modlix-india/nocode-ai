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

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# The broad-campaign scale vocabulary - the ONLY values ``scale`` may carry.
# Set only for broad (non-local) areas; neighbourhoods/localities omit it.
# platform_mapping derives BROAD_SCALES from this, so a scale accepted here
# can never be missed by the pincode-backfill exemption (PR #91 B6: "region"
# was mapped to a Meta location_type but absent from BROAD_SCALES, so a
# region-scale area got its polygon shrunk to one postal code).
Scale = Literal["city", "state", "region", "country"]


class MetaGeoLocation(BaseModel):
    """Meta Ads targeting handle - Meta's native adgeolocation shape.

    ``type``/``key``/``name`` are Meta's own field names (a ``geo_locations``
    entry is ``{key, name, …}`` bucketed by ``type``)."""

    # type & key are both required & non-empty - Meta's ``geo_locations``
    # targeting rejects an entry that lacks either (``key`` is the geo id it
    # buckets under ``type``), so a handle without a key is unusable and must
    # never be built: the mapper attaches ``area.meta`` only after a key lookup
    # succeeds, else leaves it None (lat/lng radius targeting instead).
    # ``type`` is a free string, not an enum: Meta can return finer types
    # (subcity, neighborhood, …) and rejecting those would lose a location.
    type: str = Field(min_length=1)
    key: str = Field(min_length=1)
    name: str | None = None  # display label only - Meta does not require it


class GoogleGeoLocation(BaseModel):
    """Google Ads targeting handle - a geo target constant.

    Uses Google's own field name: the identifier is the constant's
    ``resourceName`` (``geoTargetConstants/{id}``), not the bare numeric ``id``.

    ``resourceName`` is required & non-empty - it IS the geo-target constant
    Google Ads targets on; without it the handle is meaningless, so the mapper
    attaches ``area.google`` only when a constant resolves, else leaves it None
    (lat/lng proximity targeting instead)."""

    resourceName: str = Field(min_length=1)
    name: str | None = None  # canonical display name only - not required by the API

    @field_validator("resourceName")
    @classmethod
    def _as_resource_name(cls, v: str) -> str:
        """Normalize to the ``geoTargetConstants/{id}`` resource-name form."""
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
    scale: Scale | None = None
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
    scale: Scale | None = Field(
        None,
        description=(
            "'city' | 'state' | 'region' | 'country' when the area is that broad "
            "(e.g. 'add Mumbai' is a city). Omit for neighbourhoods/localities."
        ),
    )
    place_type: str | None = Field(
        None,
        description=(
            "Raw ad-platform geo type (e.g. 'Neighborhood', 'City', 'Postal code'). "
            "Pass through verbatim from the message when present; never invent it."
        ),
    )


class DeleteLocation(BaseModel):
    """Params for ``delete_location`` - which area to remove (1-based)."""

    index: int = Field(ge=1, description="1-based position in the current targeting list.")


def is_local_business(business_scale: str) -> bool:
    """Check if the resolved scale is local (radial-scan targeting applies)."""
    return (business_scale or "").strip().lower() == "local"
