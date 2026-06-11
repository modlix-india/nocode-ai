"""Google Ads Recommendations Adapter.

Queries native campaign optimisation suggestions directly from the Google Ads API.

Supported recommendation types are expressed as the ``RecommendationType`` enum so
that callers get IDE auto-complete, typo safety, and a single source of truth.

Reference:
  https://developers.google.com/google-ads/api/docs/recommendations
  https://developers.google.com/google-ads/api/reference/rpc/v23/RecommendationTypeEnum.RecommendationType
  https://developers.google.com/google-ads/api/fields/v21/recommendation
  https://developers.google.com/google-ads/api/reference/rpc/v23/Recommendation.LowerTargetRoasRecommendation (currentAverageTargetMicros is int64 micros — 1,000,000 = 1.0 ROAS ratio)
  https://ads-developers.googleblog.com/2025/03/upcoming-change-to-lowertargetroas.html (confirms micros format is correct as of the April 23 2025 API fix)
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Any, Dict, List, Optional

from app.agents.adzump.adapters.google.client import google_ads_client

logger = logging.getLogger(__name__)


# Enum: every recommendation type that is fully supported by the Google Ads API


class RecommendationType(str, Enum):
    """Google Ads API recommendation types mapped to their GAQL string literals.

    Pass any member directly into a GAQL ``WHERE recommendation.type =`` filter
    without extra mapping. Types without a parser return an empty ``details`` dict.
    """

    # Budget
    CAMPAIGN_BUDGET = "CAMPAIGN_BUDGET"
    """Fix campaigns currently limited by budget."""

    MOVE_UNUSED_BUDGET = "MOVE_UNUSED_BUDGET"
    """Move unused budget from an unconstrained campaign to a constrained one."""

    FORECASTING_CAMPAIGN_BUDGET = "FORECASTING_CAMPAIGN_BUDGET"
    """Fix campaigns expected to become budget-limited in the future."""

    MARGINAL_ROI_CAMPAIGN_BUDGET = "MARGINAL_ROI_CAMPAIGN_BUDGET"
    """Adjust campaign budget to maximise ROI."""

    # Keywords
    KEYWORD = "KEYWORD"
    """Add new keywords to an ad group."""

    USE_BROAD_MATCH_KEYWORD = "USE_BROAD_MATCH_KEYWORD"
    """Switch to broad match for conversion-based campaigns with Smart Bidding."""

    # KEYWORD_MATCH_TYPE is deprecated — use USE_BROAD_MATCH_KEYWORD instead.
    KEYWORD_MATCH_TYPE = "KEYWORD_MATCH_TYPE"
    """Deprecated. Use USE_BROAD_MATCH_KEYWORD instead."""

    # Ads & creatives
    TEXT_AD = "TEXT_AD"
    """Add ad suggestions to an ad group."""

    RESPONSIVE_SEARCH_AD = "RESPONSIVE_SEARCH_AD"
    """Add a new responsive search ad."""

    RESPONSIVE_SEARCH_AD_ASSET = "RESPONSIVE_SEARCH_AD_ASSET"
    """Add assets to an existing responsive search ad."""

    RESPONSIVE_SEARCH_AD_IMPROVE_AD_STRENGTH = (
        "RESPONSIVE_SEARCH_AD_IMPROVE_AD_STRENGTH"
    )
    """Improve the Ad Strength rating of a responsive search ad."""

    IMPROVE_DEMAND_GEN_AD_STRENGTH = "IMPROVE_DEMAND_GEN_AD_STRENGTH"
    """Improve ad strength in Demand Gen campaigns."""

    # Bidding strategies
    TARGET_CPA_OPT_IN = "TARGET_CPA_OPT_IN"
    """Switch to Target CPA bidding."""

    TARGET_ROAS_OPT_IN = "TARGET_ROAS_OPT_IN"
    """Switch to Target ROAS bidding."""

    MAXIMIZE_CONVERSIONS_OPT_IN = "MAXIMIZE_CONVERSIONS_OPT_IN"
    """Switch to Maximise Conversions bidding."""

    MAXIMIZE_CONVERSION_VALUE_OPT_IN = "MAXIMIZE_CONVERSION_VALUE_OPT_IN"
    """Switch to Maximise Conversion Value bidding."""

    MAXIMIZE_CLICKS_OPT_IN = "MAXIMIZE_CLICKS_OPT_IN"
    """Switch to Maximise Clicks bidding."""

    ENHANCED_CPC_OPT_IN = "ENHANCED_CPC_OPT_IN"
    """Enable Enhanced CPC on a manual CPC campaign."""

    RAISE_TARGET_CPA = "RAISE_TARGET_CPA"
    """Raise the target CPA to get more conversions."""

    RAISE_TARGET_CPA_BID_TOO_LOW = "RAISE_TARGET_CPA_BID_TOO_LOW"
    """Raise target CPA when it is too low and conversions are very few or zero."""

    LOWER_TARGET_ROAS = "LOWER_TARGET_ROAS"
    """Lower target ROAS to increase conversion volume."""

    SET_TARGET_CPA = "SET_TARGET_CPA"
    """Set a target CPA for campaigns that currently have none."""

    SET_TARGET_ROAS = "SET_TARGET_ROAS"
    """Set a target ROAS for campaigns that currently have none."""

    FORECASTING_SET_TARGET_CPA = "FORECASTING_SET_TARGET_CPA"
    """Set a target CPA ahead of a forecasted seasonal traffic spike."""

    FORECASTING_SET_TARGET_ROAS = "FORECASTING_SET_TARGET_ROAS"
    """Switch to Target ROAS and raise budget ahead of a forecasted seasonal event."""

    # Ad rotation
    OPTIMIZE_AD_ROTATION = "OPTIMIZE_AD_ROTATION"
    """Switch to optimised ad rotation."""

    # Targeting & reach
    DISPLAY_EXPANSION_OPT_IN = "DISPLAY_EXPANSION_OPT_IN"
    """Expand reach by enabling Display Expansion for a Search campaign."""

    SEARCH_PARTNERS_OPT_IN = "SEARCH_PARTNERS_OPT_IN"
    """Expand reach via Google Search Partners."""

    CUSTOM_AUDIENCE_OPT_IN = "CUSTOM_AUDIENCE_OPT_IN"
    """Create and apply a custom audience."""

    REFRESH_CUSTOMER_MATCH_LIST = "REFRESH_CUSTOMER_MATCH_LIST"
    """Update a Customer Match list that hasn't been refreshed in 90+ days."""

    # Assets / extensions
    LEAD_FORM_ASSET = "LEAD_FORM_ASSET"
    """Add a lead form asset to a campaign."""

    CALLOUT_ASSET = "CALLOUT_ASSET"
    """Add callout assets at campaign or account level."""

    SITELINK_ASSET = "SITELINK_ASSET"
    """Add sitelink assets at campaign or account level."""

    CALL_ASSET = "CALL_ASSET"
    """Add call assets at campaign or account level."""

    DYNAMIC_IMAGE_EXTENSION_OPT_IN = "DYNAMIC_IMAGE_EXTENSION_OPT_IN"
    """Enable dynamic image extensions on the account."""

    # Performance Max
    PERFORMANCE_MAX_OPT_IN = "PERFORMANCE_MAX_OPT_IN"
    """Create Performance Max campaigns in this account."""

    IMPROVE_PERFORMANCE_MAX_AD_STRENGTH = "IMPROVE_PERFORMANCE_MAX_AD_STRENGTH"
    """Improve asset group strength of a Performance Max campaign to 'Excellent'."""

    PERFORMANCE_MAX_FINAL_URL_OPT_IN = "PERFORMANCE_MAX_FINAL_URL_OPT_IN"
    """Enable Final URL expansion for Performance Max campaigns."""

    # Campaign upgrades
    UPGRADE_SMART_SHOPPING_CAMPAIGN_TO_PERFORMANCE_MAX = (
        "UPGRADE_SMART_SHOPPING_CAMPAIGN_TO_PERFORMANCE_MAX"
    )
    """Upgrade a Smart Shopping campaign to Performance Max."""

    UPGRADE_LOCAL_CAMPAIGN_TO_PERFORMANCE_MAX = (
        "UPGRADE_LOCAL_CAMPAIGN_TO_PERFORMANCE_MAX"
    )
    """Upgrade a legacy Local campaign to Performance Max."""

    MIGRATE_DYNAMIC_SEARCH_ADS_CAMPAIGN_TO_PERFORMANCE_MAX = (
        "MIGRATE_DYNAMIC_SEARCH_ADS_CAMPAIGN_TO_PERFORMANCE_MAX"
    )
    """Migrate a Dynamic Search Ads campaign to Performance Max."""

    # Shopping
    SHOPPING_ADD_AGE_GROUP = "SHOPPING_ADD_AGE_GROUP"
    """Add the age group attribute to demoted Shopping offers."""

    SHOPPING_ADD_COLOR = "SHOPPING_ADD_COLOR"
    """Add a colour attribute to demoted Shopping offers."""

    SHOPPING_ADD_GENDER = "SHOPPING_ADD_GENDER"
    """Add a gender attribute to demoted Shopping offers."""

    SHOPPING_ADD_GTIN = "SHOPPING_ADD_GTIN"
    """Add a GTIN (Global Trade Item Number) to demoted Shopping offers."""

    SHOPPING_ADD_MORE_IDENTIFIERS = "SHOPPING_ADD_MORE_IDENTIFIERS"
    """Add more product identifiers to demoted Shopping offers."""

    SHOPPING_ADD_SIZE = "SHOPPING_ADD_SIZE"
    """Add a size attribute to demoted Shopping offers."""

    SHOPPING_ADD_PRODUCTS_TO_CAMPAIGN = "SHOPPING_ADD_PRODUCTS_TO_CAMPAIGN"
    """Add products so that a Shopping campaign can serve."""

    SHOPPING_FIX_DISAPPROVED_PRODUCTS = "SHOPPING_FIX_DISAPPROVED_PRODUCTS"
    """Fix disapproved products in a Shopping campaign."""

    SHOPPING_TARGET_ALL_OFFERS = "SHOPPING_TARGET_ALL_OFFERS"
    """Create a catch-all campaign targeting all Shopping offers."""

    SHOPPING_FIX_SUSPENDED_MERCHANT_CENTER_ACCOUNT = (
        "SHOPPING_FIX_SUSPENDED_MERCHANT_CENTER_ACCOUNT"
    )
    """Resolve Merchant Center account suspension issues."""

    SHOPPING_FIX_MERCHANT_CENTER_ACCOUNT_SUSPENSION_WARNING = (
        "SHOPPING_FIX_MERCHANT_CENTER_ACCOUNT_SUSPENSION_WARNING"
    )
    """Resolve Merchant Center account suspension *warning* issues."""

    SHOPPING_MIGRATE_REGULAR_SHOPPING_CAMPAIGN_OFFERS_TO_PERFORMANCE_MAX = (
        "SHOPPING_MIGRATE_REGULAR_SHOPPING_CAMPAIGN_OFFERS_TO_PERFORMANCE_MAX"
    )
    """Migrate Regular Shopping campaign offers to existing Performance Max campaigns."""

    # Tracking & measurement
    IMPROVE_GOOGLE_TAG_COVERAGE = "IMPROVE_GOOGLE_TAG_COVERAGE"
    """Deploy the Google Tag on more pages for better conversion tracking."""


# Per-type GAQL field configuration

# Base fields always selected regardless of type.
_BASE_FIELDS: List[str] = [
    "recommendation.resource_name",
    "recommendation.type",
    "recommendation.campaign",
    "recommendation.impact",
]

# Extra fields that are only available for specific types.
_TYPE_EXTRA_FIELDS: Dict[RecommendationType, List[str]] = {
    RecommendationType.KEYWORD: [
        "recommendation.keyword_recommendation",
    ],
    RecommendationType.CAMPAIGN_BUDGET: [
        "recommendation.campaign_budget_recommendation",
    ],
    RecommendationType.FORECASTING_CAMPAIGN_BUDGET: [
        "recommendation.forecasting_campaign_budget_recommendation",
    ],
    RecommendationType.MARGINAL_ROI_CAMPAIGN_BUDGET: [
        "recommendation.marginal_roi_campaign_budget_recommendation",
    ],
    RecommendationType.MOVE_UNUSED_BUDGET: [
        "recommendation.move_unused_budget_recommendation",
    ],
    RecommendationType.TARGET_CPA_OPT_IN: [
        "recommendation.target_cpa_opt_in_recommendation",
    ],
    RecommendationType.TARGET_ROAS_OPT_IN: [
        "recommendation.target_roas_opt_in_recommendation",
    ],
    RecommendationType.SET_TARGET_CPA: [
        "recommendation.set_target_cpa_recommendation",
    ],
    RecommendationType.SET_TARGET_ROAS: [
        "recommendation.set_target_roas_recommendation",
    ],
    RecommendationType.MAXIMIZE_CONVERSIONS_OPT_IN: [
        "recommendation.maximize_conversions_opt_in_recommendation",
    ],
    RecommendationType.MAXIMIZE_CONVERSION_VALUE_OPT_IN: [
        "recommendation.maximize_conversion_value_opt_in_recommendation",
    ],
    RecommendationType.MAXIMIZE_CLICKS_OPT_IN: [
        "recommendation.maximize_clicks_opt_in_recommendation",
    ],
    RecommendationType.RAISE_TARGET_CPA: [
        "recommendation.raise_target_cpa_recommendation",
    ],
    RecommendationType.LOWER_TARGET_ROAS: [
        "recommendation.lower_target_roas_recommendation",
    ],
    RecommendationType.RESPONSIVE_SEARCH_AD: [
        "recommendation.responsive_search_ad_recommendation",
    ],
    RecommendationType.TEXT_AD: [
        "recommendation.text_ad_recommendation",
    ],
    RecommendationType.SITELINK_ASSET: [
        "recommendation.sitelink_asset_recommendation",
    ],
    RecommendationType.CALLOUT_ASSET: [
        "recommendation.callout_asset_recommendation",
    ],
    RecommendationType.CALL_ASSET: [
        "recommendation.call_asset_recommendation",
    ],
    RecommendationType.LEAD_FORM_ASSET: [
        "recommendation.lead_form_asset_recommendation",
    ],
    RecommendationType.USE_BROAD_MATCH_KEYWORD: [
        "recommendation.use_broad_match_keyword_recommendation",
    ],
}


# Google Recommendations Adapter
class GoogleRecommendationsAdapter:
    """Fetches and parses native Google Ads recommendations via GAQL.

    Returns normalised dicts with a common schema (``resource_name``, ``type``,
    ``campaign``, ``impact``, ``details``, ``raw``). Type-specific fields live
    in ``details``; the raw API payload is always preserved under ``raw``.
    """

    def __init__(self) -> None:
        self.client = google_ads_client

    # Public API
    async def fetch_recommendations(
        self,
        customer_id: str,
        login_customer_id: str,
        client_code: str,
        auth_headers: dict,
        recommendation_type: RecommendationType = RecommendationType.KEYWORD,
    ) -> List[Dict[str, Any]]:
        """Fetch recommendations of a given type for a Google Ads customer.

        Returns normalised dicts with keys: ``resource_name``, ``type``,
        ``campaign``, ``impact`` (base/potential metrics), ``details``
        (type-specific parsed fields), and ``raw`` (full API payload).
        """
        query = self._build_query(recommendation_type)
        logger.info(
            "fetch_recommendations: type=%s customer=%s",
            recommendation_type.value,
            customer_id,
        )

        try:
            results = await self.client.search(
                query=query,
                customer_id=customer_id,
                login_customer_id=login_customer_id,
                client_code=client_code,
                auth_headers=auth_headers,
            )
        except Exception:
            logger.exception(
                "Failed to query Google Ads API (type=%s customer=%s)",
                recommendation_type.value,
                customer_id,
            )
            return []

        parsed: List[Dict[str, Any]] = []
        for row in results:
            rec = row.get("recommendation")
            if not rec:
                continue

            normalised = self._normalise(rec, recommendation_type)
            if normalised:
                parsed.append(normalised)

        logger.info(
            "fetch_recommendations_done: type=%s count=%d",
            recommendation_type.value,
            len(parsed),
        )
        return parsed

    async def fetch_multiple_types(
        self,
        customer_id: str,
        login_customer_id: str,
        client_code: str,
        auth_headers: dict,
        recommendation_types: Optional[List[RecommendationType]] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch several recommendation types in a single call.

        Returns ``{type_value: [parsed_recs]}``. Defaults to a commonly-used
        subset (keyword, budget, bidding, ad, sitelink) when ``types`` is None.
        """
        if recommendation_types is None:
            recommendation_types = [
                RecommendationType.KEYWORD,
                RecommendationType.CAMPAIGN_BUDGET,
                RecommendationType.TARGET_CPA_OPT_IN,
                RecommendationType.TARGET_ROAS_OPT_IN,
                RecommendationType.RESPONSIVE_SEARCH_AD,
                RecommendationType.SITELINK_ASSET,
            ]

        results = await asyncio.gather(
            *[
                self.fetch_recommendations(
                    customer_id=customer_id,
                    login_customer_id=login_customer_id,
                    client_code=client_code,
                    auth_headers=auth_headers,
                    recommendation_type=rec_type,
                )
                for rec_type in recommendation_types
            ]
        )
        return {
            rec_type.value: recs
            for rec_type, recs in zip(recommendation_types, results)
        }

    # ------------------------------------------------------------------
    # Query builder
    # ------------------------------------------------------------------

    def _build_query(self, recommendation_type: RecommendationType) -> str:
        """Return a GAQL SELECT statement for the given recommendation type."""
        extra = _TYPE_EXTRA_FIELDS.get(recommendation_type, [])
        all_fields = _BASE_FIELDS + extra
        select_clause = ",\n  ".join(all_fields)
        return (
            f"SELECT\n  {select_clause}\n"
            f"FROM recommendation\n"
            f"WHERE recommendation.type = '{recommendation_type.value}'"
        )

    # ------------------------------------------------------------------
    # Normalisation
    # ------------------------------------------------------------------

    def _normalise(
        self, rec: dict, recommendation_type: RecommendationType
    ) -> Optional[Dict[str, Any]]:
        """Convert a raw API recommendation dict into the common schema."""
        normalised: Dict[str, Any] = {
            "resource_name": rec.get("resourceName", ""),
            "type": recommendation_type.value,
            "campaign": rec.get("campaign", ""),
            "impact": self._parse_impact(rec.get("impact", {})),
            "details": {},
            "raw": rec,
        }

        parser = self._PARSERS.get(recommendation_type)
        if parser:
            details = parser(self, rec)
            if details is None:
                # Parser signals the record is invalid / incomplete — skip it.
                return None
            normalised["details"] = details

        return normalised

    # ------------------------------------------------------------------
    # Impact helper
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_impact(impact: dict) -> Dict[str, Any]:
        """Extract ``{base, potential}`` performance metrics from a recommendation impact block."""

        def _metrics(m: dict) -> Dict[str, Any]:
            return {
                "impressions": m.get("impressions"),
                "clicks": m.get("clicks"),
                "conversions": m.get("conversions"),
                "cost_micros": m.get("costMicros"),
            }

        return {
            "base": _metrics(impact.get("baseMetrics", {})),
            "potential": _metrics(impact.get("potentialMetrics", {})),
        }

    # ------------------------------------------------------------------
    # Type-specific parsers
    # ------------------------------------------------------------------

    def _parse_keyword(self, rec: dict) -> Optional[Dict[str, Any]]:
        kw_rec = rec.get("keywordRecommendation", {})
        kw = kw_rec.get("keyword", {})
        text = (kw.get("text") or "").strip().lower()
        if not text:
            return None  # skip malformed records

        bid_micros = kw_rec.get("recommendedCpcBidMicros")
        return {
            "keyword": text,
            "match_type": kw.get("matchType", "BROAD"),
            "recommended_cpc": float(bid_micros) / 1_000_000 if bid_micros else 0.0,
            "is_native_recommendation": True,
        }

    def _parse_campaign_budget(self, rec: dict) -> Dict[str, Any]:
        br = rec.get("campaignBudgetRecommendation", {})
        return {
            "current_budget": _micros_to_currency(br.get("currentBudgetAmountMicros")),
            "recommended_budget": _micros_to_currency(
                br.get("recommendedBudgetAmountMicros")
            ),
        }

    def _parse_forecasting_campaign_budget(self, rec: dict) -> Dict[str, Any]:
        br = rec.get("forecastingCampaignBudgetRecommendation", {})
        return {
            "current_budget": _micros_to_currency(br.get("currentBudgetAmountMicros")),
            "recommended_budget": _micros_to_currency(
                br.get("recommendedBudgetAmountMicros")
            ),
        }

    def _parse_marginal_roi_campaign_budget(self, rec: dict) -> Dict[str, Any]:
        br = rec.get("marginalRoiCampaignBudgetRecommendation", {})
        return {
            "current_budget": _micros_to_currency(br.get("currentBudgetAmountMicros")),
            "recommended_budget": _micros_to_currency(
                br.get("recommendedBudgetAmountMicros")
            ),
        }

    def _parse_move_unused_budget(self, rec: dict) -> Dict[str, Any]:
        mr = rec.get("moveUnusedBudgetRecommendation", {})
        return {
            "excess_campaign_budget": mr.get("excessCampaignBudget", ""),
            "recommended_budget": _micros_to_currency(
                mr.get("recommendedBudgetAmountMicros")
            ),
        }

    def _parse_target_cpa_opt_in(self, rec: dict) -> Dict[str, Any]:
        tr = rec.get("targetCpaOptInRecommendation", {})
        return {
            "recommended_target_cpa": _micros_to_currency(
                tr.get("recommendedTargetCpaMicros")
            ),
            "options": tr.get("options", []),
        }

    def _parse_target_roas_opt_in(self, rec: dict) -> Dict[str, Any]:
        tr = rec.get("targetRoasOptInRecommendation", {})
        return {
            "recommended_target_roas": tr.get("recommendedTargetRoas"),
            "required_campaign_budget": _micros_to_currency(
                tr.get("requiredCampaignBudgetAmountMicros")
            ),
        }

    def _parse_set_target_cpa(self, rec: dict) -> Dict[str, Any]:
        tr = rec.get("setTargetCpaRecommendation", {})
        return {
            "recommended_target_cpa": _micros_to_currency(tr.get("targetCpaMicros")),
            "required_campaign_budget": _micros_to_currency(
                tr.get("requiredCampaignBudgetAmountMicros")
            ),
        }

    def _parse_set_target_roas(self, rec: dict) -> Dict[str, Any]:
        tr = rec.get("setTargetRoasRecommendation", {})
        return {
            "recommended_target_roas": tr.get("targetRoas"),
            "required_campaign_budget": _micros_to_currency(
                tr.get("requiredCampaignBudgetAmountMicros")
            ),
        }

    def _parse_maximize_conversions(self, rec: dict) -> Dict[str, Any]:
        mr = rec.get("maximizeConversionsOptInRecommendation", {})
        return {
            "recommended_budget": _micros_to_currency(
                mr.get("recommendedBudgetAmountMicros")
            ),
        }

    def _parse_maximize_conversion_value(self, rec: dict) -> Dict[str, Any]:
        mr = rec.get("maximizeConversionValueOptInRecommendation", {})
        return {
            "recommended_budget": _micros_to_currency(
                mr.get("recommendedBudgetAmountMicros")
            ),
        }

    def _parse_maximize_clicks(self, rec: dict) -> Dict[str, Any]:
        mr = rec.get("maximizeClicksOptInRecommendation", {})
        return {
            "recommended_budget": _micros_to_currency(
                mr.get("recommendedBudgetAmountMicros")
            ),
            "recommended_bid_ceiling": _micros_to_currency(
                mr.get("recommendedBidCeilingMicros")
            ),
        }

    def _parse_raise_target_cpa(self, rec: dict) -> Dict[str, Any]:
        rr = rec.get("raiseTargetCpaRecommendation", {})
        return {
            "recommended_target_cpa_multiplier": rr.get(
                "recommendedTargetCpaMultiplier"
            ),
        }

    def _parse_lower_target_roas(self, rec: dict) -> Dict[str, Any]:
        lr = rec.get("lowerTargetRoasRecommendation", {})
        current_micros = lr.get("currentAverageTargetMicros")
        multiplier = lr.get("recommendedTargetMultiplier")

        recommended_roas = None
        if current_micros is not None and multiplier is not None:
            try:
                current_roas = float(current_micros) / 1_000_000
                recommended_roas = current_roas * float(multiplier)
            except (ValueError, TypeError):
                pass

        return {
            "current_average_target_roas": float(current_micros) / 1_000_000
            if current_micros is not None
            else None,
            "recommended_target_multiplier": float(multiplier)
            if multiplier is not None
            else None,
            "recommended_target_roas": recommended_roas,
        }

    def _parse_responsive_search_ad(self, rec: dict) -> Dict[str, Any]:
        rr = rec.get("responsiveSearchAdRecommendation", {})
        return {
            "ad": rr.get("ad", {}),
        }

    def _parse_text_ad(self, rec: dict) -> Dict[str, Any]:
        tr = rec.get("textAdRecommendation", {})
        return {
            "ad": tr.get("ad", {}),
        }

    def _parse_sitelink_asset(self, rec: dict) -> Dict[str, Any]:
        sr = rec.get("sitelinkAssetRecommendation", {})
        return {
            "recommended_sitelink_assets": sr.get("recommendedSitelinkAssets", []),
        }

    def _parse_callout_asset(self, rec: dict) -> Dict[str, Any]:
        cr = rec.get("calloutAssetRecommendation", {})
        return {
            "recommended_callout_assets": cr.get("recommendedCalloutAssets", []),
        }

    def _parse_call_asset(self, rec: dict) -> Dict[str, Any]:
        cr = rec.get("callAssetRecommendation", {})
        return {
            "recommended_call_assets": cr.get("recommendedCallAssets", []),
        }

    def _parse_use_broad_match_keyword(self, rec: dict) -> Dict[str, Any]:
        br = rec.get("useBroadMatchKeywordRecommendation", {})
        return {
            "keyword": br.get("keyword", {}),
            "suggested_keywords": br.get("suggestedKeywords", []),
            "recommended_campaign_budget": _micros_to_currency(
                br.get("recommendedCampaignBudgetAmountMicros")
            ),
        }

    # ------------------------------------------------------------------
    # Parser dispatch table  (built after the method definitions above)
    # ------------------------------------------------------------------

    _PARSERS: Dict[RecommendationType, Any] = {}  # populated below


# Populate the dispatch table after the class body so we can reference methods.
GoogleRecommendationsAdapter._PARSERS = {
    RecommendationType.KEYWORD: GoogleRecommendationsAdapter._parse_keyword,
    RecommendationType.CAMPAIGN_BUDGET: GoogleRecommendationsAdapter._parse_campaign_budget,
    RecommendationType.FORECASTING_CAMPAIGN_BUDGET: (
        GoogleRecommendationsAdapter._parse_forecasting_campaign_budget
    ),
    RecommendationType.MARGINAL_ROI_CAMPAIGN_BUDGET: (
        GoogleRecommendationsAdapter._parse_marginal_roi_campaign_budget
    ),
    RecommendationType.MOVE_UNUSED_BUDGET: GoogleRecommendationsAdapter._parse_move_unused_budget,
    RecommendationType.TARGET_CPA_OPT_IN: GoogleRecommendationsAdapter._parse_target_cpa_opt_in,
    RecommendationType.TARGET_ROAS_OPT_IN: GoogleRecommendationsAdapter._parse_target_roas_opt_in,
    RecommendationType.SET_TARGET_CPA: GoogleRecommendationsAdapter._parse_set_target_cpa,
    RecommendationType.SET_TARGET_ROAS: GoogleRecommendationsAdapter._parse_set_target_roas,
    RecommendationType.MAXIMIZE_CONVERSIONS_OPT_IN: (
        GoogleRecommendationsAdapter._parse_maximize_conversions
    ),
    RecommendationType.MAXIMIZE_CONVERSION_VALUE_OPT_IN: (
        GoogleRecommendationsAdapter._parse_maximize_conversion_value
    ),
    RecommendationType.MAXIMIZE_CLICKS_OPT_IN: (
        GoogleRecommendationsAdapter._parse_maximize_clicks
    ),
    RecommendationType.RAISE_TARGET_CPA: GoogleRecommendationsAdapter._parse_raise_target_cpa,
    RecommendationType.LOWER_TARGET_ROAS: GoogleRecommendationsAdapter._parse_lower_target_roas,
    RecommendationType.RESPONSIVE_SEARCH_AD: (
        GoogleRecommendationsAdapter._parse_responsive_search_ad
    ),
    RecommendationType.TEXT_AD: GoogleRecommendationsAdapter._parse_text_ad,
    RecommendationType.SITELINK_ASSET: GoogleRecommendationsAdapter._parse_sitelink_asset,
    RecommendationType.CALLOUT_ASSET: GoogleRecommendationsAdapter._parse_callout_asset,
    RecommendationType.CALL_ASSET: GoogleRecommendationsAdapter._parse_call_asset,
    RecommendationType.USE_BROAD_MATCH_KEYWORD: (
        GoogleRecommendationsAdapter._parse_use_broad_match_keyword
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _micros_to_currency(micros: Optional[Any]) -> Optional[float]:
    """Convert a micros integer (or string) to a float currency value.

    Returns ``None`` when *micros* is absent. Returns 0.0 for zero micros.
    """
    if micros is None:
        return None
    return float(micros) / 1_000_000


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

google_recommendations_adapter = GoogleRecommendationsAdapter()
