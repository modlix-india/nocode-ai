"""Google campaign-type (channel) capability matrix — Level-2 capabilities.

Two capability levels exist and must not be conflated:
  Level 1 — ``PlatformCapabilities`` (optimization/provider_base.py): which
            platforms (Google/Meta/…) the engine supports. Unchanged by this file.
  Level 2 — this file: within Google, what each ``advertising_channel_type``
            supports.

Google-internal: Meta/TikTok never import it. Self-contained leaf — it defines
its own enums and stores Google API enum names as plain strings; a unit test
asserts each is a real ``BiddingStrategyType`` / ``RecommendationType`` member so
the two can never drift.

Sources (official Google Ads documentation):
  Bidding strategy types / assignment:
    https://developers.google.com/google-ads/api/docs/campaigns/bidding/strategy-types
    https://developers.google.com/google-ads/api/docs/campaigns/bidding/assign-strategies
  Impression-share availability by campaign type:
    https://support.google.com/google-ads/answer/7103314
  Performance Max (asset groups, no keywords, campaign-level reporting):
    https://developers.google.com/google-ads/api/docs/performance-max/overview
    https://developers.google.com/google-ads/api/performance-max/campaign-reporting
  search_term_view (Search campaigns only):
    https://developers.google.com/google-ads/api/fields/v22/search_term_view
  Recommendation types:
    https://developers.google.com/google-ads/api/docs/recommendations
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Optional

from app.agents.adzump.recommendations.models.google import ChannelType


class ISMetricFamily(str, Enum):
    """Which impression-share metric family is meaningful for the channel."""

    SEARCH = "SEARCH"  # search_* IS metrics
    CONTENT = "CONTENT"  # content_* IS metrics (Display)
    SEARCH_CAMPAIGN_LEVEL_ONLY = "SEARCH_CAMPAIGN_LEVEL_ONLY"  # PMax: no lost-IS split
    NONE = "NONE"


class CreativeKind(str, Enum):
    """Creative structure (if any) the channel exposes for analysis."""

    RSA = "RSA"  # responsive search ads
    ASSET_GROUP = "ASSET_GROUP"  # Performance Max asset groups
    NONE = "NONE"


@dataclass(frozen=True)
class ChannelCapabilities:
    """Immutable capability record for one Google ``advertising_channel_type``.

    All fields default to the conservative baseline (nothing supported) — a channel
    row sets only what it has, and a new field touches only the rows that differ."""

    channel: ChannelType
    display_label: str  # human label for "not applicable" messages
    supports_keywords: bool = False
    supports_search_terms: bool = False
    supports_quality_score: bool = False
    is_metric_family: ISMetricFamily = ISMetricFamily.NONE
    supports_is_constraint_diagnosis: bool = False  # lost-IS split available & meaningful
    valid_bidding_strategies: FrozenSet[str] = frozenset()  # BiddingStrategyType names
    allowed_bidding_rec_types: FrozenSet[str] = frozenset()  # RecommendationType names we surface
    creative_kind: CreativeKind = CreativeKind.NONE
    supports_bidding_decision_tree: bool = False  # the Search-calibrated maturity tree applies


# Bidding rec-type building blocks (RecommendationType enum names).
_TARGET_ADJUST = frozenset(
    {"SET_TARGET_CPA", "SET_TARGET_ROAS", "RAISE_TARGET_CPA", "LOWER_TARGET_ROAS"}
)
_SEARCH_BIDDING_RECS = _TARGET_ADJUST | {
    "TARGET_CPA_OPT_IN",
    "TARGET_ROAS_OPT_IN",
    "MAXIMIZE_CONVERSIONS_OPT_IN",
    "MAXIMIZE_CONVERSION_VALUE_OPT_IN",
}
# PMax allows only MAXIMIZE_CONVERSIONS / MAXIMIZE_CONVERSION_VALUE, with targets
# set inside the strategy — not standalone tCPA/tROAS strategies. So we permit
# target adjustments plus switching between those two strategies; both opt-ins are
# legitimate PMax native recs and must not be dropped by the step-5 safety belt.
_PMAX_BIDDING_RECS = _TARGET_ADJUST | {
    "MAXIMIZE_CONVERSIONS_OPT_IN",
    "MAXIMIZE_CONVERSION_VALUE_OPT_IN",
}


_CAPABILITIES: dict[ChannelType, ChannelCapabilities] = {
    ChannelType.SEARCH: ChannelCapabilities(
        channel=ChannelType.SEARCH,
        display_label="Search",
        supports_keywords=True,
        supports_search_terms=True,
        supports_quality_score=True,
        is_metric_family=ISMetricFamily.SEARCH,
        supports_is_constraint_diagnosis=True,
        valid_bidding_strategies=frozenset(
            {
                "MANUAL_CPC",
                "TARGET_SPEND",  # Maximize Clicks
                "MAXIMIZE_CONVERSIONS",
                "TARGET_CPA",
                "MAXIMIZE_CONVERSION_VALUE",
                "TARGET_ROAS",
                "TARGET_IMPRESSION_SHARE",
            }
        ),
        allowed_bidding_rec_types=frozenset(_SEARCH_BIDDING_RECS),
        creative_kind=CreativeKind.RSA,
        supports_bidding_decision_tree=True,
    ),
    ChannelType.PERFORMANCE_MAX: ChannelCapabilities(
        channel=ChannelType.PERFORMANCE_MAX,
        display_label="Performance Max",
        supports_keywords=False,
        supports_search_terms=False,
        supports_quality_score=False,
        is_metric_family=ISMetricFamily.SEARCH_CAMPAIGN_LEVEL_ONLY,
        supports_is_constraint_diagnosis=False,
        valid_bidding_strategies=frozenset(
            {"MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE"}
        ),
        allowed_bidding_rec_types=frozenset(_PMAX_BIDDING_RECS),
        creative_kind=CreativeKind.ASSET_GROUP,
        supports_bidding_decision_tree=False,
    ),
    ChannelType.SHOPPING: ChannelCapabilities(
        channel=ChannelType.SHOPPING,
        display_label="Shopping",
        supports_keywords=False,
        supports_search_terms=False,
        supports_quality_score=False,
        is_metric_family=ISMetricFamily.SEARCH,
        supports_is_constraint_diagnosis=True,
        valid_bidding_strategies=frozenset(
            {"MANUAL_CPC", "TARGET_SPEND", "TARGET_ROAS", "MAXIMIZE_CONVERSION_VALUE"}
        ),
        # Tree off — Google native recs only. Belt stays permissive (Google won't
        # emit a strategy illegal for Shopping); its real job is the PMax case.
        allowed_bidding_rec_types=frozenset(_SEARCH_BIDDING_RECS),
        creative_kind=CreativeKind.NONE,
        supports_bidding_decision_tree=False,
    ),
    ChannelType.DISPLAY: ChannelCapabilities(
        channel=ChannelType.DISPLAY,
        display_label="Display",
        supports_keywords=False,
        supports_search_terms=False,
        supports_quality_score=False,
        is_metric_family=ISMetricFamily.CONTENT,
        supports_is_constraint_diagnosis=True,  # content family has the lost-IS split
        valid_bidding_strategies=frozenset(
            {"MANUAL_CPC", "MAXIMIZE_CONVERSIONS", "TARGET_CPA", "TARGET_ROAS", "TARGET_SPEND"}
        ),
        allowed_bidding_rec_types=frozenset(_SEARCH_BIDDING_RECS),
        creative_kind=CreativeKind.NONE,
        supports_bidding_decision_tree=False,
    ),
    ChannelType.DEMAND_GEN: ChannelCapabilities(
        channel=ChannelType.DEMAND_GEN,
        display_label="Demand Gen",
        # Audience/creative channel — no keywords/search-terms/QS/IS (defaults).
        # But it IS a Smart Bidding channel (verified: Target CPA/ROAS, Maximize
        # Conversions, Maximize Conversion Value — Search+DemandGen only — and
        # Maximize Clicks). So native bidding recs MUST flow; the Search-calibrated
        # tree stays off. Creative analysis is a future phase.
        valid_bidding_strategies=frozenset(
            {
                "TARGET_CPA",
                "TARGET_ROAS",
                "MAXIMIZE_CONVERSIONS",
                "MAXIMIZE_CONVERSION_VALUE",
                "TARGET_SPEND",
            }
        ),
        allowed_bidding_rec_types=frozenset(_SEARCH_BIDDING_RECS),
    ),
}

# Conservative default for VIDEO / MULTI_CHANNEL (App) / HOTEL / LOCAL_SERVICES /
# TRAVEL / UNKNOWN: universal layer only (budget native recs + conversion health,
# gated elsewhere). No keywords, IS, creative, or bidding recs from us — we never
# fake a Search signal a channel doesn't have. (Demand Gen is NOT here — it has an
# explicit row above because it supports Smart Bidding.)
_CONSERVATIVE_LABELS: dict[ChannelType, str] = {
    ChannelType.VIDEO: "Video",
    ChannelType.MULTI_CHANNEL: "App",
    ChannelType.HOTEL: "Hotel",
    ChannelType.LOCAL_SERVICES: "Local Services",
    ChannelType.TRAVEL: "Travel",
}


def _conservative(channel: ChannelType) -> ChannelCapabilities:
    # Every capability defaults to off — only the label is channel-specific.
    return ChannelCapabilities(
        channel=channel,
        display_label=_CONSERVATIVE_LABELS.get(channel, "this campaign type"),
    )


def _normalize_channel(campaign_type: Optional[str]) -> ChannelType:
    """Raw ``advertising_channel_type`` → ChannelType; unrecognized → UNKNOWN."""
    if not campaign_type:
        return ChannelType.UNKNOWN
    try:
        return ChannelType(str(campaign_type).strip().upper())
    except ValueError:
        return ChannelType.UNKNOWN


def get_capabilities(campaign_type: Optional[str]) -> ChannelCapabilities:
    """Normalize a raw ``advertising_channel_type`` and return its capabilities;
    unknown/unsupported channels get the conservative default (budget native-rec
    passthrough + conversion health only)."""
    channel = _normalize_channel(campaign_type)
    return _CAPABILITIES.get(channel) or _conservative(channel)
