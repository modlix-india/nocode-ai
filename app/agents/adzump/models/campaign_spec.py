"""CampaignSpec - the typed model over ``session.context["campaign_spec"]``.

Storage stays a plain dict (integration contract, HLD/LLD doc §4.6); this
model is the lenient parse over it. Slice 1a types the offer fields; the
remaining keys ride ``extra="allow"`` until slice 3 finishes the typing.

``offer_state`` is the migration-aware single read for offer fields: readers
use it instead of key-existence checks, so legacy ``*_declined="true"``
sessions and enum-format sessions answer identically.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.agents.adzump.models.offer_state import OfferState

# Offer field -> the legacy marker key it replaces. from_stored pops the
# legacy key; _apply_field canonicalizes legacy writes; offer_state reads both.
LEGACY_DECLINED_KEYS: dict[str, str] = {
    "competitive_analysis": "competitive_analysis_declined",
    "competitor_creatives": "competitor_creatives_declined",
    "instagram": "ig_page_declined",
}
OFFER_FIELDS: tuple[str, ...] = tuple(LEGACY_DECLINED_KEYS)


def offer_state(spec: dict | None, field: str) -> OfferState:
    """Migration-aware read: the enum value when stored, else the legacy
    ``*_declined`` marker mapped through ``OfferState.from_legacy``."""
    spec = spec or {}
    raw = spec.get(field)
    if raw is not None:
        try:
            return OfferState(str(raw))
        except ValueError:
            return OfferState.UNSET
    return OfferState.from_legacy(spec.get(LEGACY_DECLINED_KEYS[field]))


class CampaignSpec(BaseModel):
    model_config = ConfigDict(extra="allow", use_enum_values=False)

    # user answers (free-text; _field_traceable-gated)
    platform: str = ""
    duration: str = ""
    budget: str = ""
    location: str = ""
    # account hierarchy (ids; account_names-gated)
    parent_account: str = ""
    account: str = ""
    fb_page: str = ""
    ig_page: str = ""  # id present == linked (like every account field)
    # offers (replace the *_declined="true" strings)
    competitive_analysis: OfferState = OfferState.UNSET
    competitor_creatives: OfferState = OfferState.UNSET
    # only UNSET/DECLINED; linked = ig_page set (D12)
    instagram: OfferState = OfferState.UNSET
    # lifecycle (not a user answer)
    campaign_status: str = ""

    @classmethod
    def from_stored(cls, raw: dict | None) -> "CampaignSpec":
        """Lenient parse: legacy ``*_declined="true"`` -> DECLINED (legacy keys
        dropped from the model), unknown keys carried via extra="allow",
        missing -> defaults."""
        data = dict(raw or {})
        for field, legacy in LEGACY_DECLINED_KEYS.items():
            marker = data.pop(legacy, None)
            if marker is not None and field not in data:
                state = OfferState.from_legacy(marker)
                if state is not OfferState.UNSET:
                    data[field] = state
        return cls.model_validate(data)

    def to_stored(self) -> dict:
        """Plain dict back into ``session.context["campaign_spec"]``. Enums as
        .value with UNSET offers dropped, empty-string defaults dropped - so
        'key present == answered' reads stay true. Never re-emits legacy keys."""
        stored: dict = {}
        for key, value in self.model_dump(mode="json").items():
            if key in OFFER_FIELDS:
                if value != OfferState.UNSET.value:
                    stored[key] = value
            elif value != "":
                stored[key] = value
        return stored
