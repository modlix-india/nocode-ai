"""Place - the location primitive. Pure pydantic leaf: importable anywhere
(product schema, location tools, targeting builders) without a cycle."""

from __future__ import annotations

from pydantic import BaseModel


class Place(BaseModel):
    """The confirmed campaign location - scraped address refined by geocode/map-pin."""

    address: str = ""
    lat: float | None = None
    lng: float | None = None
    country_code: str = ""  # ISO-3166 alpha-2, from the geocode
    country_geo_constant: str = ""  # Google Ads geoTargetConstants/{id} for the country
    display_name: str = ""  # map-pin label "<business>, <address>"
