"""Google Ads recommendation models, enums, and conversion-health cluster.

Imports from base.py only. Conversion-health models live here (not base) because
each platform's conversion concepts differ — Meta gets its own in meta.py.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Optional, Literal
from pydantic import AfterValidator, Field

from app.agents.adzump.adapters.google.google_ads_limits import (
    KEYWORD_MAX_LENGTH,
    LIMITS,
)
from app.agents.adzump.recommendations.models.base import (
    CamelModel,
    WorkflowItem,
    BaseOptimizationFields,
    BaseCampaignRecommendation,
    CheckSeverity,
    ConfidenceLevel,
    ScopeType,
    Platform,
)


class HealthLabel(str, Enum):
    """Campaign or conversion health status label."""

    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    DEGRADED = "DEGRADED"
    HEALTHY = "HEALTHY"


def _validate_age_range(v: str) -> str:
    if v not in LIMITS.AGE.VALID_RANGES:
        raise ValueError(
            f"Invalid age range '{v}'. Must be one of: {sorted(LIMITS.AGE.VALID_RANGES)}"
        )
    return v


def _validate_gender(v: str) -> str:
    if v not in LIMITS.GENDER.VALID_TYPES:
        raise ValueError(
            f"Invalid gender '{v}'. Must be one of: {sorted(LIMITS.GENDER.VALID_TYPES)}"
        )
    return v


# Validated against google_ads_limits SSOT — change values there, not here.
AgeRangeType = Annotated[str, AfterValidator(_validate_age_range)]
GenderType = Annotated[str, AfterValidator(_validate_gender)]


class ChannelType(str, Enum):
    """Google ``advertising_channel_type`` values; UNKNOWN is the safe default
    for old or unrecognized records. Consumed by the channel capability matrix."""

    SEARCH = "SEARCH"
    PERFORMANCE_MAX = "PERFORMANCE_MAX"
    SHOPPING = "SHOPPING"
    DISPLAY = "DISPLAY"
    VIDEO = "VIDEO"
    DEMAND_GEN = "DEMAND_GEN"
    MULTI_CHANNEL = "MULTI_CHANNEL"  # App campaigns report as MULTI_CHANNEL
    HOTEL = "HOTEL"
    LOCAL_SERVICES = "LOCAL_SERVICES"
    TRAVEL = "TRAVEL"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def _missing_(cls, value: Any) -> ChannelType:
        if not value:
            return cls.UNKNOWN
        val_upper = str(value).strip().upper()
        for member in cls:
            if member.value == val_upper:
                return member
        return cls.UNKNOWN


class CampaignStatus(str, Enum):
    """Google Ads API CampaignStatus."""

    UNSPECIFIED = "UNSPECIFIED"
    UNKNOWN = "UNKNOWN"
    ENABLED = "ENABLED"
    PAUSED = "PAUSED"
    REMOVED = "REMOVED"

    @classmethod
    def _missing_(cls, value: Any) -> CampaignStatus:
        if not value:
            return cls.UNKNOWN
        val_upper = str(value).strip().upper()
        for member in cls:
            if member.value == val_upper:
                return member
        return cls.UNKNOWN


class ConversionSignalStatus(str, Enum):
    """Light indicator showing recent conversion volume trends in Google Ads."""

    STABLE = "stable"
    DROPPING = "dropping"
    UNTRACKED = "untracked"
    INSUFFICIENT_HISTORY = "insufficient_history"

    @classmethod
    def _missing_(cls, value: Any) -> ConversionSignalStatus:
        if not value:
            return cls.INSUFFICIENT_HISTORY
        val_lower = str(value).strip().lower()
        for member in cls:
            if member.value == val_lower:
                return member
        return cls.INSUFFICIENT_HISTORY


class ConstraintType(str, Enum):
    """Google Ads budget and bidding constraint states."""

    BUDGET_CONSTRAINED = "BUDGET_CONSTRAINED"
    BID_CONSTRAINED = "BID_CONSTRAINED"
    MIXED_CONSTRAINT = "MIXED_CONSTRAINT"
    NONE = "NONE"

    @classmethod
    def _missing_(cls, value: Any) -> ConstraintType:
        if not value:
            return cls.NONE
        val_upper = str(value).strip().upper()
        for member in cls:
            if member.value == val_upper:
                return member
        return cls.NONE


class MatchType(str, Enum):
    """Google Ads keyword match types."""

    EXACT = "EXACT"
    PHRASE = "PHRASE"
    BROAD = "BROAD"
    NEAR_EXACT = "NEAR_EXACT"
    NEAR_PHRASE = "NEAR_PHRASE"

    @classmethod
    def _missing_(cls, value: Any) -> MatchType:
        if not value:
            return cls.PHRASE
        val_upper = str(value).strip().upper()
        for member in cls:
            if member.value == val_upper:
                return member
        return cls.PHRASE


class BiddingStrategyType(str, Enum):
    """Google Ads bidding strategies."""

    UNSPECIFIED = "UNSPECIFIED"
    UNKNOWN = "UNKNOWN"
    MANUAL_CPC = "MANUAL_CPC"
    ENHANCED_CPC = "ENHANCED_CPC"
    MAXIMIZE_CONVERSIONS = "MAXIMIZE_CONVERSIONS"
    MAXIMIZE_CONVERSION_VALUE = "MAXIMIZE_CONVERSION_VALUE"
    TARGET_CPA = "TARGET_CPA"
    TARGET_ROAS = "TARGET_ROAS"
    TARGET_SPEND = "TARGET_SPEND"
    TARGET_IMPRESSION_SHARE = "TARGET_IMPRESSION_SHARE"

    @classmethod
    def _missing_(cls, value: Any) -> BiddingStrategyType:
        if not value:
            return cls.UNKNOWN
        val_upper = str(value).strip().upper()
        for member in cls:
            if member.value == val_upper:
                return member
        return cls.UNKNOWN


class ConversionSignal(CamelModel):
    """Light campaign signal used to decide whether to run health checks."""

    status: ConversionSignalStatus
    delta_pct: Optional[float] = None
    window: str = "14d_vs_prior_14d"
    min_volume_met: bool = False
    conversions_recent: float = 0.0
    conversions_prior: float = 0.0
    campaign_age_days: Optional[int] = None
    evaluated_at: str = ""

    def to_context_line(self) -> str:
        """Render as a single passive line for the agent's dynamic context."""
        if self.status == ConversionSignalStatus.UNTRACKED:
            return "Conversion tracking: untracked — no conversion actions configured"
        if self.status == ConversionSignalStatus.INSUFFICIENT_HISTORY:
            age = (
                f" (campaign {self.campaign_age_days}d old)"
                if self.campaign_age_days
                else ""
            )
            return f"Conversion tracking: — insufficient history{age}"
        if self.status == ConversionSignalStatus.DROPPING:
            return (
                f"Conversion tracking: dropping — "
                f"{self.conversions_recent:.0f} conv last 14d vs "
                f"{self.conversions_prior:.0f} prior 14d "
                f"({self.delta_pct:+.0f}%)"
            )
        return (
            f"Conversion tracking: stable — {self.conversions_recent:.0f} conv last 14d"
        )


class ConversionHealthCheck(CamelModel):
    """Single diagnostic rule evaluation result."""

    check_id: str
    passed: bool
    severity: CheckSeverity
    title: str
    description: str
    affected_entity_ids: list[str] = []
    metadata: dict[str, Any] = {}


class ConversionFixStep(CamelModel):
    """Sequential instruction step for applying a manual recommendation."""

    step_number: int
    instruction: str
    code: Optional[str] = None


class ConversionFixGuide(CamelModel):
    """Detailed troubleshooting manual for non-automated recommendations."""

    title: str
    summary: str
    steps: list[ConversionFixStep] = []
    estimated_time: str = ""
    can_auto_apply: bool = False


class ConversionFixCard(WorkflowItem):
    """Actionable ticket resolving a failing conversion check."""

    check_id: str
    campaign_id: str
    title: str
    rationale: str
    severity: CheckSeverity
    fix_type: str  # tag, goal, config, signal
    optiscore_delta: float = 0.0
    can_auto_apply: bool = False
    tag_snippet: Optional[dict[str, Any]] = None
    mutation_payload: Optional[dict[str, Any]] = None
    implementation_guide: Optional[ConversionFixGuide] = None

    def compute_fingerprint(self, campaign_id: str) -> str:
        return f"card:GOOGLE:{campaign_id}:{str(self.check_id).strip()}"


class ConversionHealthReport(CamelModel):
    """Aggregate report containing all conversion-health checks and tickets."""

    campaign_id: str
    health_label: HealthLabel
    health_score: int
    conversion_signal: Optional[ConversionSignal] = None
    checks: list[ConversionHealthCheck] = []
    fix_cards: list[ConversionFixCard] = []
    evaluated_at: str = ""

    def to_context_summary(self) -> str:
        """Render a 1-2 line summary for the agent's dynamic context."""
        failing = [c for c in self.checks if not c.passed]
        if not failing:
            return f"Conversion health: {self.health_label} ({self.health_score}/100)"
        titles = "; ".join(c.title for c in failing[:2])
        more = f" +{len(failing) - 2} more" if len(failing) > 2 else ""
        return (
            f"Conversion health: {self.health_label} ({self.health_score}/100) — "
            f"{titles}{more}"
        )


class GoogleKeywordRecommendation(WorkflowItem):
    text: str = Field(..., max_length=KEYWORD_MAX_LENGTH)
    match_type: Optional[MatchType] = Field(default=MatchType.PHRASE, kw_only=True)
    recommendation: Optional[str] = Field(default="ADD", kw_only=True)
    ad_group_id: Optional[str] = None
    ad_group_name: Optional[str] = None
    criterion_id: Optional[str] = None
    resource_name: Optional[str] = None
    score: Optional[float] = None
    metrics: Optional[dict[str, Any]] = None
    quality_score: Optional[int] = None
    origin: str = "KEYWORD"

    def compute_fingerprint(self, campaign_id: str) -> str:
        return (
            f"kw:GOOGLE:{campaign_id}"
            f":{self.ad_group_id or ''}"
            f":{str(self.text).strip().lower()}"
            f":{str(self.match_type or '').upper()}"
            f":{str(self.recommendation or '').upper()}"
            f":{self.criterion_id or ''}"
        )


class BudgetBiddingRecommendation(WorkflowItem):
    campaign_id: str
    campaign_name: str
    scope: ScopeType
    portfolio_strategy_id: Optional[str] = None
    portfolio_strategy_name: Optional[str] = None
    current_strategy: BiddingStrategyType
    bidding_rec_type: Optional[str] = None
    bidding_rec_rationale: Optional[str] = None
    bidding_confidence: ConfidenceLevel = ConfidenceLevel.LOW
    bidding_blocked_reason: Optional[str] = None
    current_budget: float = 0.0
    recommended_budget: Optional[float] = None
    budget_rec_type: Optional[str] = None
    budget_rec_rationale: Optional[str] = None
    budget_confidence: ConfidenceLevel = ConfidenceLevel.LOW
    apply_order: list[str] = []
    learning_phase_warning: Optional[str] = None
    constraint_type: ConstraintType = ConstraintType.NONE
    google_rec_confirmed: bool = False
    blocking_issues: list[str] = []
    pacing_warning: Optional[str] = None
    move_unused_budget_signal: bool = False
    metric_freshness_warning: Optional[str] = None
    currency_code: str = "INR"

    def compute_fingerprint(self, campaign_id: str) -> str:
        return (
            f"bb:GOOGLE:{campaign_id}"
            f":{str(self.scope).strip().upper()}"
            f":{str(self.bidding_rec_type or '').upper()}"
            f":{str(self.budget_rec_type or '').upper()}"
        )


class GoogleAgeRecommendation(WorkflowItem):
    ad_group_id: Optional[str] = None
    ad_group_name: Optional[str] = None
    age_range: Optional[AgeRangeType] = None
    recommendation: Optional[str] = Field(default="REMOVE", kw_only=True)
    resource_name: Optional[str] = None

    def compute_fingerprint(self, campaign_id: str) -> str:
        return (
            f"age:GOOGLE:{campaign_id}"
            f":{self.ad_group_id or ''}"
            f":{str(self.age_range or '').upper()}"
            f":{str(self.recommendation or '').upper()}"
        )


class GoogleGenderRecommendation(WorkflowItem):
    ad_group_id: Optional[str] = None
    ad_group_name: Optional[str] = None
    gender: Optional[GenderType] = None
    gender_type: Optional[GenderType] = None
    recommendation: Optional[str] = Field(default="REMOVE", kw_only=True)
    resource_name: Optional[str] = None

    def compute_fingerprint(self, campaign_id: str) -> str:
        return (
            f"gender:GOOGLE:{campaign_id}"
            f":{self.ad_group_id or ''}"
            f":{str(self.gender or self.gender_type or '').upper()}"
            f":{str(self.recommendation or '').upper()}"
        )


class CampaignOverview(CamelModel):
    """Top-level campaign metadata and counts."""

    campaign_id: str = ""
    status: CampaignStatus = CampaignStatus.UNKNOWN
    campaign_type: ChannelType = ChannelType.UNKNOWN
    currency_code: str = "INR"
    spend: float = 0.0
    conversions: float = 0.0
    adset_count: int = 0
    ad_count: int = 0
    campaign_name: Optional[str] = None


class GoogleOptimizationFields(BaseOptimizationFields):
    """Google-specific campaign recommendation fields."""

    overview: Optional[CampaignOverview] = None
    keywords: list[GoogleKeywordRecommendation] = []
    negative_keywords: list[GoogleKeywordRecommendation] = []
    budget_bidding: Optional[BudgetBiddingRecommendation] = None
    conversion_health: Optional[ConversionHealthReport] = None
    age: list[GoogleAgeRecommendation] = []
    gender: list[GoogleGenderRecommendation] = []


class GoogleCampaignRecommendation(BaseCampaignRecommendation):
    """Stored Google campaign recommendation bundle."""

    platform: Literal[Platform.GOOGLE] = Platform.GOOGLE
    campaign_status: CampaignStatus = CampaignStatus.UNKNOWN
    fields: GoogleOptimizationFields = Field(default_factory=GoogleOptimizationFields)
