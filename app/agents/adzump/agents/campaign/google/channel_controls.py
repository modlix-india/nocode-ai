"""Where a Demand Gen ad is allowed to show - the rules behind ``DemandGenBuild
.channel_controls``. Demand Gen only, unlike ``audience/``, which serves several channels.

Eligibility is a property of (ad type x surface), so it lives here as data: change the ad
type and the surfaces follow. One definition, read by the tool, the panel and the emitter.

Always the granular selected_channels. Google's presets on the other side of that oneof
(ALL_CHANNELS, ALL_OWNED_AND_OPERATED_CHANNELS) would enable placements the ad type cannot
serve on - dead reach that reads as underperformance rather than as a config mistake.

  https://developers.google.com/google-ads/api/docs/demand-gen/channel-controls
  https://support.google.com/google-ads/answer/17140672  (image eligibility)
  https://support.google.com/google-ads/answer/17141078  (video eligibility)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AdType(str, Enum):
    """Google names formats by message type; the Help Center names them by what the
    advertiser uploads. "Image only" and "multi-asset" are the same format."""

    IMAGE = "DemandGenMultiAssetAdInfo"
    VIDEO = "DemandGenVideoResponsiveAdInfo"
    # CAROUSEL (DemandGenCarouselAdInfo) is absent: Google's format-by-placement table has no
    # carousel column, and neither channel enum carries format information, so its
    # eligibility is unsourced. Add it with one.


AD_TYPE_LABEL = {AdType.IMAGE: "Image ads", AdType.VIDEO: "Video ads"}


def ad_type_for(creative: dict | None) -> AdType:
    """The one resolver - panel, toggle and emitter all branch on this, and they would
    disagree the moment video lands if any of them guessed instead of passing the slot."""
    return AdType.IMAGE


_BOTH = frozenset({AdType.IMAGE, AdType.VIDEO})


@dataclass(frozen=True)
class Surface:
    key: str  # the API field on DemandGenSelectedChannels
    label: str
    serves: frozenset[AdType]


# The two exclusions are mirror images, so neither format alone covers every surface.
# `maps` is absent: a v24 field, and the client speaks v23 where sending it fails the mutate.
# `youtubeInFeed` covers Home, Search and Watch Next - Google publishes no mapping, but all
# three share eligibility, so the rollup does not change the answer.
# Google Video Partners has no API field, so it is not represented.
SURFACES: tuple[Surface, ...] = (
    Surface("youtubeInFeed", "YouTube feed", _BOTH),
    Surface("youtubeShorts", "YouTube Shorts", _BOTH),
    Surface("youtubeInStream", "YouTube in-stream", frozenset({AdType.VIDEO})),
    Surface("discover", "Discover", _BOTH),
    Surface("gmail", "Gmail", frozenset({AdType.IMAGE})),
    Surface("display", "Display Network", _BOTH),
)

_BY_KEY = {s.key: s for s in SURFACES}


def locked_reason(surface: Surface, ad_type: AdType) -> str:
    """Why this surface cannot be enabled, or "" when it can."""
    if ad_type in surface.serves:
        return ""
    return f"{AD_TYPE_LABEL[ad_type].lower()} cannot serve here"


def defaults(ad_type: AdType) -> dict[str, bool]:
    """Everything this ad type can serve on. Narrowing is the user's call, not ours."""
    return {s.key: ad_type in s.serves for s in SURFACES}


def normalize(controls: dict | None, ad_type: AdType) -> dict[str, bool]:
    """A stored selection as the API shape, with unknown keys dropped and ineligible ones
    forced off - a selection saved under a different ad type would otherwise keep enabling a
    placement that cannot serve."""
    stored = controls or {}
    return {
        s.key: bool(stored.get(s.key, True)) if ad_type in s.serves else False
        for s in SURFACES
    }


def toggle(
    controls: dict | None, key: str, enabled: bool, ad_type: AdType
) -> tuple[dict | None, str]:
    """(new controls, error). The error alone when the change is refused."""
    surface = _BY_KEY.get(key)
    if surface is None:
        return None, f"'{key}' is not a Demand Gen surface."
    if enabled and (reason := locked_reason(surface, ad_type)):
        return None, f"{surface.label} cannot be turned on - {reason}."

    updated = normalize(controls, ad_type)
    updated[key] = enabled
    if not any(updated.values()):
        return (
            None,
            f"{surface.label} is the last one on - the ad needs somewhere to show.",
        )
    return updated, ""
