"""Google Ads API conversion-related enums — verified from official documentation.

Every enum value in this file is sourced directly from the Google Ads API
proto definitions and reference documentation. Source URLs are cited per enum.

Reference (verified May 2026):
  https://developers.google.com/google-ads/api/docs/conversions/getting-started
  https://developers.google.com/google-ads/api/reference/rpc/v23/ConversionTrackingStatusEnum.ConversionTrackingStatus
  https://developers.google.com/google-ads/api/reference/rpc/v23/ConversionActionCategoryEnum.ConversionActionCategory
  https://developers.google.com/google-ads/api/reference/rpc/v23/ConversionActionStatusEnum.ConversionActionStatus
  https://developers.google.com/google-ads/api/reference/rpc/v23/ConversionActionTypeEnum.ConversionActionType
  https://developers.google.com/google-ads/api/reference/rpc/v23/ConversionActionCountingTypeEnum.ConversionActionCountingType
  https://developers.google.com/google-ads/api/reference/rpc/v23/AttributionModelEnum.AttributionModel
  https://developers.google.com/google-ads/api/docs/query/date-ranges  (GAQL date range enums)
"""

from __future__ import annotations
from enum import Enum

# GAQL date range enums
# Source: https://developers.google.com/google-ads/api/docs/query/date-ranges


class QueryWindow(str, Enum):
    """Valid GAQL DURING clause date range enum values."""

    TODAY = "TODAY"
    YESTERDAY = "YESTERDAY"
    LAST_7_DAYS = "LAST_7_DAYS"
    LAST_14_DAYS = "LAST_14_DAYS"
    LAST_30_DAYS = "LAST_30_DAYS"
    LAST_BUSINESS_WEEK = "LAST_BUSINESS_WEEK"
    THIS_WEEK_SUN_TODAY = "THIS_WEEK_SUN_TODAY"
    THIS_WEEK_MON_TODAY = "THIS_WEEK_MON_TODAY"
    LAST_WEEK_SUN_SAT = "LAST_WEEK_SUN_SAT"
    LAST_WEEK_MON_SUN = "LAST_WEEK_MON_SUN"
    THIS_MONTH = "THIS_MONTH"
    LAST_MONTH = "LAST_MONTH"


# ConversionTrackingStatus
# Source: https://developers.google.com/google-ads/api/reference/rpc/v23/ConversionTrackingStatusEnum.ConversionTrackingStatus


class ConversionTrackingStatus(str, Enum):
    """Conversion tracking status of a customer account."""

    UNSPECIFIED = "UNSPECIFIED"
    UNKNOWN = "UNKNOWN"
    NOT_CONVERSION_TRACKED = "NOT_CONVERSION_TRACKED"
    CONVERSION_TRACKING_MANAGED_BY_SELF = "CONVERSION_TRACKING_MANAGED_BY_SELF"
    CONVERSION_TRACKING_MANAGED_BY_THIS_MANAGER = (
        "CONVERSION_TRACKING_MANAGED_BY_THIS_MANAGER"
    )
    CONVERSION_TRACKING_MANAGED_BY_ANOTHER_MANAGER = (
        "CONVERSION_TRACKING_MANAGED_BY_ANOTHER_MANAGER"
    )


# Statuses that indicate conversion tracking IS configured (not absent).
CONVERSION_TRACKED_STATUSES: frozenset[ConversionTrackingStatus] = frozenset(
    {
        ConversionTrackingStatus.CONVERSION_TRACKING_MANAGED_BY_SELF,
        ConversionTrackingStatus.CONVERSION_TRACKING_MANAGED_BY_THIS_MANAGER,
        ConversionTrackingStatus.CONVERSION_TRACKING_MANAGED_BY_ANOTHER_MANAGER,
    }
)


# ConversionActionStatus
# Source: https://developers.google.com/google-ads/api/reference/rpc/v23/ConversionActionStatusEnum.ConversionActionStatus


class ConversionActionStatus(str, Enum):
    """Status of a conversion action."""

    UNSPECIFIED = "UNSPECIFIED"
    UNKNOWN = "UNKNOWN"
    ENABLED = "ENABLED"
    REMOVED = "REMOVED"
    HIDDEN = "HIDDEN"  # Analytics goals imported but not enabled in Google Ads


# ConversionActionCategory
# Source: https://developers.google.com/google-ads/api/reference/rpc/v23/ConversionActionCategoryEnum.ConversionActionCategory


class ConversionActionCategory(str, Enum):
    """Category of conversions associated with a ConversionAction."""

    UNSPECIFIED = "UNSPECIFIED"
    UNKNOWN = "UNKNOWN"
    DEFAULT = "DEFAULT"
    PAGE_VIEW = "PAGE_VIEW"
    PURCHASE = "PURCHASE"
    SIGNUP = "SIGNUP"
    LEAD = "LEAD"
    DOWNLOAD = "DOWNLOAD"
    ADD_TO_CART = "ADD_TO_CART"
    BEGIN_CHECKOUT = "BEGIN_CHECKOUT"
    SUBSCRIBE_PAID = "SUBSCRIBE_PAID"
    PHONE_CALL_LEAD = "PHONE_CALL_LEAD"
    IMPORTED_LEAD = "IMPORTED_LEAD"
    SUBMIT_LEAD_FORM = "SUBMIT_LEAD_FORM"
    BOOK_APPOINTMENT = "BOOK_APPOINTMENT"
    REQUEST_QUOTE = "REQUEST_QUOTE"
    GET_DIRECTIONS = "GET_DIRECTIONS"
    OUTBOUND_CLICK = "OUTBOUND_CLICK"
    CONTACT = "CONTACT"
    ENGAGEMENT = "ENGAGEMENT"
    STORE_VISIT = "STORE_VISIT"
    STORE_SALE = "STORE_SALE"
    QUALIFIED_LEAD = "QUALIFIED_LEAD"
    CONVERTED_LEAD = "CONVERTED_LEAD"
    YOUTUBE_FOLLOW_ON_VIEWS = "YOUTUBE_FOLLOW_ON_VIEWS"  # Added in v23


# Category groupings used by verify_conversion_health check logic.
# Source rationale: ecommerce actions should use MANY_PER_CLICK counting
# (multiple items in one session), leads should use ONE_PER_CLICK
# (one lead per click is the intended business outcome).
ECOMMERCE_CATEGORIES: frozenset[ConversionActionCategory] = frozenset(
    {
        ConversionActionCategory.PURCHASE,
        ConversionActionCategory.ADD_TO_CART,
        ConversionActionCategory.BEGIN_CHECKOUT,
        ConversionActionCategory.STORE_SALE,
        ConversionActionCategory.SUBSCRIBE_PAID,
    }
)

LEAD_CATEGORIES: frozenset[ConversionActionCategory] = frozenset(
    {
        ConversionActionCategory.LEAD,
        ConversionActionCategory.SIGNUP,
        ConversionActionCategory.CONTACT,
        ConversionActionCategory.SUBMIT_LEAD_FORM,
        ConversionActionCategory.PHONE_CALL_LEAD,
        ConversionActionCategory.BOOK_APPOINTMENT,
        ConversionActionCategory.REQUEST_QUOTE,
        ConversionActionCategory.IMPORTED_LEAD,
        ConversionActionCategory.QUALIFIED_LEAD,
        ConversionActionCategory.CONVERTED_LEAD,
    }
)

# ConversionActionType
# Source: https://developers.google.com/google-ads/api/docs/conversions/categories
#         https://developers.google.com/google-ads/api/reference/rpc/v23/ConversionActionTypeEnum.ConversionActionType


class ConversionActionType(str, Enum):
    """Type of a conversion action."""

    UNSPECIFIED = "UNSPECIFIED"
    UNKNOWN = "UNKNOWN"
    AD_CALL = "AD_CALL"
    CLICK_TO_CALL = "CLICK_TO_CALL"
    FIREBASE_ANDROID_FIRST_OPEN = "FIREBASE_ANDROID_FIRST_OPEN"
    FIREBASE_ANDROID_IN_APP_PURCHASE = "FIREBASE_ANDROID_IN_APP_PURCHASE"
    FIREBASE_ANDROID_CUSTOM = "FIREBASE_ANDROID_CUSTOM"
    FIREBASE_IOS_FIRST_OPEN = "FIREBASE_IOS_FIRST_OPEN"
    FIREBASE_IOS_IN_APP_PURCHASE = "FIREBASE_IOS_IN_APP_PURCHASE"
    FIREBASE_IOS_CUSTOM = "FIREBASE_IOS_CUSTOM"
    GOOGLE_PLAY_DOWNLOAD = "GOOGLE_PLAY_DOWNLOAD"
    GOOGLE_PLAY_IN_APP_PURCHASE = "GOOGLE_PLAY_IN_APP_PURCHASE"
    UPLOAD_CALLS = "UPLOAD_CALLS"
    UPLOAD_CLICKS = "UPLOAD_CLICKS"
    WEBPAGE = "WEBPAGE"
    WEBPAGE_ONCLICK = "WEBPAGE_ONCLICK"
    WEBSITE_CALL = "WEBSITE_CALL"
    STORE_SALES_DIRECT_UPLOAD = "STORE_SALES_DIRECT_UPLOAD"
    STORE_SALES = "STORE_SALES"
    FLOODLIGHT_ACTION = "FLOODLIGHT_ACTION"
    FLOODLIGHT_TRANSACTION = "FLOODLIGHT_TRANSACTION"
    SALESFORCE = "SALESFORCE"
    THIRD_PARTY_APP_ANALYTICS_ANDROID = "THIRD_PARTY_APP_ANALYTICS_ANDROID"
    THIRD_PARTY_APP_ANALYTICS_IOS = "THIRD_PARTY_APP_ANALYTICS_IOS"
    ANDROID_APP_PRE_REGISTRATION = "ANDROID_APP_PRE_REGISTRATION"
    ANDROID_INSTALLS_ALL_OTHER_APPS = "ANDROID_INSTALLS_ALL_OTHER_APPS"
    FLOODLIGHT_ACTION_360 = "FLOODLIGHT_ACTION_360"
    FLOODLIGHT_TRANSACTION_360 = "FLOODLIGHT_TRANSACTION_360"
    GOOGLE_ATTRIBUTION = "GOOGLE_ATTRIBUTION"
    STORE_SALES_SALESFORCE = "STORE_SALES_SALESFORCE"
    STORE_SALES_CRM = "STORE_SALES_CRM"
    APP_STORE_SUBSCRIPTION = "APP_STORE_SUBSCRIPTION"
    RECURRING_SUBSCRIPTION = "RECURRING_SUBSCRIPTION"


# Conversion action type groupings — tag vs API tracking

# Types that require a website tag (gtag / GTM) to be deployed.
# These are the only types for which tag_snippets should be populated.
# API-tracked types (UPLOAD_CLICKS, STORE_SALES_*, FIREBASE_*, etc.)
# never have tag_snippets — that is expected behaviour, not a missing tag.
# Source: https://developers.google.com/google-ads/api/docs/conversions/categories
WEBSITE_TAG_TYPES: frozenset[ConversionActionType] = frozenset(
    {
        ConversionActionType.WEBPAGE,
        ConversionActionType.WEBPAGE_ONCLICK,
        ConversionActionType.WEBSITE_CALL,
    }
)

# Types that use API/offline upload — no tag required or expected.
# Google automatically sets counting_type = MANY_PER_CLICK for UPLOAD_CLICKS
# regardless of category, so the wrong_counting check must skip these.
# Source: https://developers.google.com/google-ads/api/docs/conversions/create-conversion-actions
API_UPLOAD_TYPES: frozenset[ConversionActionType] = frozenset(
    {
        ConversionActionType.UPLOAD_CLICKS,
        ConversionActionType.UPLOAD_CALLS,
        ConversionActionType.STORE_SALES_DIRECT_UPLOAD,
        ConversionActionType.STORE_SALES,
        ConversionActionType.STORE_SALES_SALESFORCE,
        ConversionActionType.STORE_SALES_CRM,
    }
)


# ConversionActionCountingType
# Source: https://developers.google.com/google-ads/api/reference/rpc/v23/ConversionActionCountingTypeEnum.ConversionActionCountingType


class ConversionActionCountingType(str, Enum):
    """How conversions are counted for a conversion action."""

    UNSPECIFIED = "UNSPECIFIED"
    UNKNOWN = "UNKNOWN"
    ONE_PER_CLICK = "ONE_PER_CLICK"
    MANY_PER_CLICK = "MANY_PER_CLICK"


# AttributionModel
# Source: https://developers.google.com/google-ads/api/reference/rpc/v23/AttributionModelEnum.AttributionModel
#         https://developers.google.com/google-ads/api/docs/conversions/troubleshooting
#         https://support.google.com/google-ads/answer/6259715
#
# IMPORTANT: As of v23 Google only supports setting TWO models:
#   GOOGLE_ADS_LAST_CLICK and GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN.
# All rule-based models (FIRST_CLICK, LINEAR, TIME_DECAY, POSITION_BASED)
# are DEPRECATED. Setting them results in CANNOT_SET_RULE_BASED_ATTRIBUTION_MODELS.
# Google has automatically migrated existing actions to data-driven attribution.
# Source: https://developers.google.com/google-ads/api/docs/conversions/troubleshooting


class AttributionModel(str, Enum):
    """Attribution model for a conversion action (v23)."""

    UNSPECIFIED = "UNSPECIFIED"
    UNKNOWN = "UNKNOWN"
    # ── Currently supported (settable) ──────────────────────────────────────
    GOOGLE_ADS_LAST_CLICK = "GOOGLE_ADS_LAST_CLICK"
    GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN = "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN"
    # ── Deprecated (read-only, returned on old actions only) ─────────────────
    GOOGLE_SEARCH_ATTRIBUTION_FIRST_CLICK = "GOOGLE_SEARCH_ATTRIBUTION_FIRST_CLICK"
    GOOGLE_SEARCH_ATTRIBUTION_LINEAR = "GOOGLE_SEARCH_ATTRIBUTION_LINEAR"
    GOOGLE_SEARCH_ATTRIBUTION_TIME_DECAY = "GOOGLE_SEARCH_ATTRIBUTION_TIME_DECAY"
    GOOGLE_SEARCH_ATTRIBUTION_POSITION_BASED = (
        "GOOGLE_SEARCH_ATTRIBUTION_POSITION_BASED"
    )
    GOOGLE_SEARCH_ATTRIBUTION_LAST_CLICK = "GOOGLE_SEARCH_ATTRIBUTION_LAST_CLICK"
    GOOGLE_DISPLAY_LAST_CLICK = "GOOGLE_DISPLAY_LAST_CLICK"
    GOOGLE_DISPLAY_LINEAR = "GOOGLE_DISPLAY_LINEAR"
    GOOGLE_DISPLAY_POSITION_BASED = "GOOGLE_DISPLAY_POSITION_BASED"
    GOOGLE_DISPLAY_TIME_DECAY = "GOOGLE_DISPLAY_TIME_DECAY"


# Deprecated attribution models — the health check flags actions still using these.
# Source: https://support.google.com/google-ads/answer/6259715
# Attribution models deprecated by Google — "first click, linear, time decay, and position-based attribution models
# switch to data-driven or last-click.
DEPRECATED_ATTRIBUTION_MODELS: frozenset[AttributionModel] = frozenset(
    {
        AttributionModel.GOOGLE_SEARCH_ATTRIBUTION_FIRST_CLICK,
        AttributionModel.GOOGLE_SEARCH_ATTRIBUTION_LINEAR,
        AttributionModel.GOOGLE_SEARCH_ATTRIBUTION_TIME_DECAY,
        AttributionModel.GOOGLE_SEARCH_ATTRIBUTION_POSITION_BASED,
    }
)


# Categories requiring conversion value for tROAS.
# Source: ConversionActionCategoryEnum proto
# If value = 0 on these, TARGET_ROAS cannot optimise toward revenue.
VALUE_REQUIRED_CATEGORIES: frozenset[ConversionActionCategory] = frozenset(
    {
        ConversionActionCategory.PURCHASE,
        ConversionActionCategory.STORE_SALE,
    }
)

# Strategies that make impression share unreliable.
# Source: https://support.google.com/google-ads/answer/7381968
# These strategies are designed to spend the full daily budget.
# search_budget_lost_impression_share is always high by design —
# not a signal of budget constraint.
MAX_SPEND_STRATEGIES: frozenset[str] = frozenset(
    {
        "MAXIMIZE_CONVERSIONS",
        "MAXIMIZE_CONVERSION_VALUE",
    }
)

# Strategies that require conversion signal for Smart Bidding.
# Source: Google Ads bidding strategy documentation
CONVERSION_DEPENDENT_STRATEGIES: frozenset[str] = frozenset(
    {
        "TARGET_CPA",
        "TARGET_ROAS",
        "MAXIMIZE_CONVERSIONS",
        "MAXIMIZE_CONVERSION_VALUE",
    }
)

# Thresholds — from Google documentation

# Smart Bidding measurement guidance — 30 conversions in last 30 days
# for Target CPA to have reliable signal.
# Source: https://support.google.com/google-ads/answer/6268632
# NOTE: measurement guidance only — not an API eligibility gate.
SMART_BIDDING_MIN_CONVERSIONS: int = 30

# Impression share data computation lag.
# Source: https://support.google.com/google-ads/answer/7103314
MAX_FRESHNESS_DAYS: int = 2

# Thresholds — practitioner conventions (no Google source)

# Minimum prior 14-day conversions before the signal delta is trusted.
# Practitioner convention — no official source.
SIGNAL_MIN_PRIOR_VOLUME: int = 20

# Conversion drop threshold for the signal tool.
# Practitioner convention — no official source.
SIGNAL_DROPPING_THRESHOLD: float = -0.20  # -20%

# Minimum campaign age before computing a signal baseline.
# Practitioner convention — no official source.
SIGNAL_MIN_CAMPAIGN_AGE_DAYS: int = 30

# Stricter drop threshold for MAX_SPEND_STRATEGIES.
# Normal budget variance in these strategies creates large swings.
# Derived from SIGNAL_DROPPING_THRESHOLD × 3.
# Practitioner convention — no official source.
SIGNAL_MAX_SPEND_DROPPING_THRESHOLD: float = SIGNAL_DROPPING_THRESHOLD * 3  # -0.60
