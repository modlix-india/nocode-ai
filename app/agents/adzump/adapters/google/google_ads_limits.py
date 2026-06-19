"""Google Ads API system limits — single source of truth (SSOT).

Self-contained leaf module: it imports nothing of ours. Every value below is
written directly from official Google documentation, with the source URL on each
section. Models (pydantic ``Field(max_length=...)`` constraints) import FROM here
today; the creative analyser/generator, the search-term analyzer, and the
campaign-creation/mutation layers will as they land — never the reverse.

System-limits index: https://developers.google.com/google-ads/api/docs/best-practices/system-limits

Double-width characters: in CJK languages (Chinese, Japanese, Korean) each
character counts as 2 toward these limits, so validators must weight them — they
are not plain ``len(text)`` checks.
(Source: https://support.google.com/google-ads/answer/7684791)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet

# Raw limits (module level, importable individually)

# Responsive Search Ads — verified 2026-06-16 against:
#   https://support.google.com/google-ads/answer/7684791  (RSA: 30/90 chars, paths 15)
HEADLINE_MAX_LENGTH = 30
DESCRIPTION_MAX_LENGTH = 90
PATH_MAX_LENGTH = 15  # RSA display-path only; PMax has no display paths

# Keyword text — verified against the API system-limits page (80 characters):
#   https://developers.google.com/google-ads/api/docs/best-practices/system-limits
#   https://developers.google.com/google-ads/api/reference/rpc/v23/KeywordInfo
KEYWORD_MAX_LENGTH = 80

# Sitelink asset — verified:
#   link_text 1-25, description1/description2 1-35
#   https://developers.google.com/google-ads/api/reference/rpc/v23/SitelinkAsset
#   https://support.google.com/google-ads/answer/2375416  (25 chars; 12 for double-width)
SITELINK_TEXT_MAX_LENGTH = 25
SITELINK_DESCRIPTION_MAX_LENGTH = 35

# Final URL length — the API documents these as BYTE lengths (not characters):
#   ads:      2084 bytes,  criteria: 2047 bytes
#   https://developers.google.com/google-ads/api/docs/best-practices/system-limits
# ``URL_MAX_LENGTH`` is the conservative single bound (the smaller, criterion
# value) so one check is safe in both contexts; use the specific constant when a
# context is known.
AD_FINAL_URL_MAX_BYTES = 2084
CRITERION_FINAL_URL_MAX_BYTES = 2047
URL_MAX_LENGTH = CRITERION_FINAL_URL_MAX_BYTES  # = 2047 (conservative)

# RSA asset counts — verified: https://support.google.com/google-ads/answer/7684791
RSA_MIN_HEADLINES, RSA_MAX_HEADLINES = 3, 15
RSA_MIN_DESCRIPTIONS, RSA_MAX_DESCRIPTIONS = 2, 4

# Performance Max asset-group requirements — verified 2026-06-17 against:
#   https://developers.google.com/google-ads/api/performance-max/asset-requirements
#   Headlines 3-15 @30 chars, long headlines 1-5 @90 chars,
#   descriptions 2-5 @90 chars, business name 1 @25 chars.
#   PMax has NO display paths (PATH_MAX_LENGTH is RSA-only).
PMAX_MIN_HEADLINES = 3
PMAX_MAX_HEADLINES = 15
PMAX_HEADLINE_MAX_LENGTH = 30  # same as RSA
PMAX_LONG_HEADLINE_MAX_LENGTH = 90
PMAX_MIN_LONG_HEADLINES = 1
PMAX_MAX_LONG_HEADLINES = 5
PMAX_DESCRIPTION_MAX_LENGTH = 90
PMAX_MIN_DESCRIPTIONS = 2
PMAX_MAX_DESCRIPTIONS = 5
PMAX_BUSINESS_NAME_MAX_LENGTH = 25

# Structured frozen-dataclass groups for ergonomic access (e.g. ``LIMITS.HEADLINES.MAX_LENGTH``)


@dataclass(frozen=True)
class _HeadlineConfig:
    """RSA headline constraints."""

    MAX_LENGTH: int = HEADLINE_MAX_LENGTH
    MIN_COUNT: int = RSA_MIN_HEADLINES
    MAX_COUNT: int = RSA_MAX_HEADLINES


@dataclass(frozen=True)
class _DescriptionConfig:
    """RSA description constraints."""

    MAX_LENGTH: int = DESCRIPTION_MAX_LENGTH
    MIN_COUNT: int = RSA_MIN_DESCRIPTIONS
    MAX_COUNT: int = RSA_MAX_DESCRIPTIONS


@dataclass(frozen=True)
class _SitelinkConfig:
    """Sitelink asset constraints.

    Source: https://developers.google.com/google-ads/api/reference/rpc/v23/SitelinkAsset
    """

    LINK_TEXT_MAX_LENGTH: int = SITELINK_TEXT_MAX_LENGTH
    DESCRIPTION_MAX_LENGTH: int = SITELINK_DESCRIPTION_MAX_LENGTH
    MIN_COUNT: int = 2
    MAX_DISPLAY_DESKTOP: int = 6
    MAX_DISPLAY_MOBILE: int = 8


@dataclass(frozen=True)
class _ProximityConfig:
    """Proximity (radius) targeting constraints.

    Source: https://developers.google.com/google-ads/api/reference/rpc/v23/ProximityInfo
    """

    MILES_TO_KM: float = 1.60934
    MIN_RADIUS_KM: int = 1
    MAX_RADIUS_KM: int = 800  # 500 miles
    VALID_UNITS: FrozenSet[str] = field(
        default_factory=lambda: frozenset({"MILES", "KILOMETERS"})
    )


@dataclass(frozen=True)
class _KeywordConfig:
    """Keyword criterion constraints.

    Source: https://developers.google.com/google-ads/api/reference/rpc/v23/KeywordInfo
    """

    MAX_LENGTH: int = KEYWORD_MAX_LENGTH
    VALID_MATCH_TYPES: FrozenSet[str] = field(
        default_factory=lambda: frozenset({"EXACT", "PHRASE", "BROAD"})
    )


@dataclass(frozen=True)
class _PMaxConfig:
    """Performance Max asset-group text constraints.

    Source: https://developers.google.com/google-ads/api/performance-max/asset-requirements
    """

    HEADLINE_MAX_LENGTH: int = PMAX_HEADLINE_MAX_LENGTH
    MIN_HEADLINES: int = PMAX_MIN_HEADLINES
    MAX_HEADLINES: int = PMAX_MAX_HEADLINES
    LONG_HEADLINE_MAX_LENGTH: int = PMAX_LONG_HEADLINE_MAX_LENGTH
    MIN_LONG_HEADLINES: int = PMAX_MIN_LONG_HEADLINES
    MAX_LONG_HEADLINES: int = PMAX_MAX_LONG_HEADLINES
    DESCRIPTION_MAX_LENGTH: int = PMAX_DESCRIPTION_MAX_LENGTH
    MIN_DESCRIPTIONS: int = PMAX_MIN_DESCRIPTIONS
    MAX_DESCRIPTIONS: int = PMAX_MAX_DESCRIPTIONS
    BUSINESS_NAME_MAX_LENGTH: int = PMAX_BUSINESS_NAME_MAX_LENGTH


@dataclass(frozen=True)
class _AgeConfig:
    """Valid age-range enum values.

    Source: https://developers.google.com/google-ads/api/reference/rpc/v21/AgeRangeTypeEnum.AgeRangeType
    """

    VALID_RANGES: FrozenSet[str] = field(
        default_factory=lambda: frozenset(
            {
                "AGE_RANGE_18_24",
                "AGE_RANGE_25_34",
                "AGE_RANGE_35_44",
                "AGE_RANGE_45_54",
                "AGE_RANGE_55_64",
                "AGE_RANGE_65_UP",
                "AGE_RANGE_UNDETERMINED",
            }
        )
    )


@dataclass(frozen=True)
class _GenderConfig:
    """Valid gender-type enum values.

    Source: https://developers.google.com/google-ads/api/reference/rpc/v21/GenderTypeEnum.GenderType
    """

    VALID_TYPES: FrozenSet[str] = field(
        default_factory=lambda: frozenset({"MALE", "FEMALE", "UNDETERMINED"})
    )


@dataclass(frozen=True)
class GoogleAdsLimits:
    """Centralized, immutable Google Ads API system limits."""

    HEADLINES: _HeadlineConfig = field(default_factory=_HeadlineConfig)
    DESCRIPTIONS: _DescriptionConfig = field(default_factory=_DescriptionConfig)
    PMAX: _PMaxConfig = field(default_factory=_PMaxConfig)
    SITELINKS: _SitelinkConfig = field(default_factory=_SitelinkConfig)
    PROXIMITY: _ProximityConfig = field(default_factory=_ProximityConfig)
    KEYWORDS: _KeywordConfig = field(default_factory=_KeywordConfig)
    AGE: _AgeConfig = field(default_factory=_AgeConfig)
    GENDER: _GenderConfig = field(default_factory=_GenderConfig)
    PATH_MAX_LENGTH: int = PATH_MAX_LENGTH
    URL_MAX_LENGTH: int = URL_MAX_LENGTH
    ASSET_FIELD_TYPE_SITELINK: str = "SITELINK"


LIMITS = GoogleAdsLimits()  # global immutable instance
