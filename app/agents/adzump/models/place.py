"""Place - the location primitive, shared across adzump.

A leaf module: it imports only pydantic, so any package can depend on it
without a cycle - the product schema (which also pulls in location models),
the location tools, and the geo-targeting payload builder all import ``Place``
from here. Nothing location- or product-specific lives here; keeping it a pure
leaf is what lets it sit under BOTH the product model and the downstream
targeting builders at once.
"""

from __future__ import annotations

from pydantic import BaseModel


class Place(BaseModel):
    """Where the business is - the single home for the confirmed campaign
    location (scraped address, then refined by geocode or map-pin confirm).
    The session's one location cache; there is no separate ``_location_meta``."""

    address: str = ""
    lat: float | None = None
    lng: float | None = None
    country_code: str = ""  # ISO-3166 alpha-2, from the geocode's country component
    country_geo_constant: str = ""  # Google Ads geo-target constant for the country (geoTargetConstants/{id})
    display_name: str = ""  # "<business>, <address>" label for the map pin (ds-v1 record)
