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


class LocationProposal(BaseModel):
    """The map confirm in flight: the detected address and the pin coords the
    backend sent, so a confirm with the pin untouched keeps the address."""

    address: str = ""
    lat: float | None = None
    lng: float | None = None

    @classmethod
    def from_stored(cls, raw: object) -> "LocationProposal | None":
        """Read session storage; a pre-2026-09-23 session stored the bare string."""
        if isinstance(raw, dict):
            return cls.model_validate(raw)
        if isinstance(raw, str) and raw:
            return cls(address=raw)
        return None

    def pin_unmoved(self, lat: float | None, lng: float | None) -> bool:
        """The map echoes the sent coords verbatim until the user drags or clicks."""
        if None in (self.lat, self.lng, lat, lng):
            return False
        return abs(self.lat - lat) < 1e-6 and abs(self.lng - lng) < 1e-6
