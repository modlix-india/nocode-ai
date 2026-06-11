"""Pydantic output models for optimization recommendations."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional, Literal, Union, Annotated
from pydantic import BaseModel, Field
from pydantic.alias_generators import to_camel


# 1. Platform-Agnostic Common Types and Enums


class Platform(str, Enum):
    GOOGLE = "GOOGLE"
    META = "META"


class RecommendationStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    APPLYING = "applying"
    APPLIED = "applied"
    FAILED = "failed"
    DISMISSED = "dismissed"
    STALE = "stale"
    SUPERSEDED = "superseded"


class CheckSeverity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class ConfidenceLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ScopeType(str, Enum):
    CAMPAIGN = "CAMPAIGN"
    PORTFOLIO = "PORTFOLIO"


class HealthLabel(str, Enum):
    """Campaign or conversion health status label."""

    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    DEGRADED = "DEGRADED"
    HEALTHY = "HEALTHY"


# 2. Base Model Classes


class CamelModel(BaseModel):
    model_config = {
        "populate_by_name": True,
        "alias_generator": to_camel,
    }


class WorkflowItem(CamelModel):
    """Base class for recommendation items carrying a workflow state."""

    status: RecommendationStatus = Field(
        default=RecommendationStatus.PENDING, kw_only=True
    )
    reviewed_at: str = Field(default="", kw_only=True)
    reviewed_by: str = Field(default="", kw_only=True)
    applied_at: str = Field(default="", kw_only=True)
    failure_reason: str = Field(default="", kw_only=True)
    fingerprint: str = Field(default="", kw_only=True)
    applied: bool = Field(default=False, kw_only=True)
    reason: Optional[str] = Field(default="", kw_only=True)
    recommendation: Optional[str] = Field(default=None, kw_only=True)

    def compute_fingerprint(self, campaign_id: str) -> str:
        """Return a deterministic identity string for this item.

        Each concrete subclass overrides this. An empty string means the item
        has no stable identity and will always be treated as new on re-runs.
        """
        return ""


class BaseOptimizationFields(CamelModel):
    """Base fields class for platform-specific recommendation fields."""

    pass


class BaseCampaignRecommendation(CamelModel):
    """Base stored campaign recommendation bundle."""

    id: Optional[str] = Field(None, alias="_id")
    platform: str
    parent_account_id: Optional[str] = ""
    account_id: Optional[str] = ""
    product_id: str = ""
    product_name: str = ""
    campaign_id: str
    campaign_name: str
    campaign_type: str = "SEARCH"
    campaign_status: str = "UNKNOWN"
    adset_count: int = 0
    ad_count: int = 0
    completed: bool = False
    active: bool = True
    source: str = "scheduler"
    fields: BaseOptimizationFields = Field(default_factory=BaseOptimizationFields)
    generated_at: str = ""
    schema_version: str = "1.0"
    # OCC token — populated by the mutation endpoint when it fetches this record from storage
    # before calling apply_mutation_results(). Set it to the DB's updatedAt value so
    # sync_mutation_result() can detect if another process wrote to the record between
    # the fetch and the apply. Leave empty to skip the OCC check (scheduler path).
    record_version: str = ""


# 3. Google Ads Specific Types, Enums and Models


AgeRangeType = Literal[
    "AGE_RANGE_18_24",
    "AGE_RANGE_25_34",
    "AGE_RANGE_35_44",
    "AGE_RANGE_45_54",
    "AGE_RANGE_55_64",
    "AGE_RANGE_65_UP",
    "AGE_RANGE_UNDETERMINED",
]

GenderType = Literal["MALE", "FEMALE", "UNDETERMINED"]


class CampaignStatus(str, Enum):
    """Google Ads API CampaignStatus."""

    UNSPECIFIED = "UNSPECIFIED"
    UNKNOWN = "UNKNOWN"
    ENABLED = "ENABLED"
    PAUSED = "PAUSED"
    REMOVED = "REMOVED"


class ConversionSignalStatus(str, Enum):
    """Light indicator showing recent conversion volume trends in Google Ads."""

    STABLE = "stable"
    DROPPING = "dropping"
    UNTRACKED = "untracked"
    INSUFFICIENT_HISTORY = "insufficient_history"


class ConstraintType(str, Enum):
    """Google Ads budget and bidding constraint states."""

    BUDGET_CONSTRAINED = "BUDGET_CONSTRAINED"
    BID_CONSTRAINED = "BID_CONSTRAINED"
    MIXED_CONSTRAINT = "MIXED_CONSTRAINT"
    NONE = "NONE"


class MatchType(str, Enum):
    """Google Ads keyword match types."""

    EXACT = "EXACT"
    PHRASE = "PHRASE"
    BROAD = "BROAD"
    NEAR_EXACT = "NEAR_EXACT"
    NEAR_PHRASE = "NEAR_PHRASE"


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
            return "Conversion tracking: ✗ untracked — no conversion actions configured"
        if self.status == ConversionSignalStatus.INSUFFICIENT_HISTORY:
            age = (
                f" (campaign {self.campaign_age_days}d old)"
                if self.campaign_age_days
                else ""
            )
            return f"Conversion tracking: — insufficient history{age}"
        if self.status == ConversionSignalStatus.DROPPING:
            return (
                f"Conversion tracking: ⚠ dropping — "
                f"{self.conversions_recent:.0f} conv last 14d vs "
                f"{self.conversions_prior:.0f} prior 14d "
                f"({self.delta_pct:+.0f}%)"
            )
        return (
            f"Conversion tracking: ✓ stable — "
            f"{self.conversions_recent:.0f} conv last 14d"
        )


class ConversionHealthCheck(CamelModel):
    check_id: str
    passed: bool
    severity: CheckSeverity
    title: str
    description: str
    affected_entity_ids: list[str] = []
    metadata: dict[str, Any] = {}


class ConversionFixStep(CamelModel):
    step_number: int
    instruction: str
    code: Optional[str] = None


class ConversionFixGuide(CamelModel):
    title: str
    summary: str
    steps: list[ConversionFixStep] = []
    estimated_time: str = ""
    can_auto_apply: bool = False


class ConversionFixCard(WorkflowItem):
    """UI fix card for a failing conversion health check."""

    check_id: str
    campaign_id: str
    title: str
    rationale: str
    severity: CheckSeverity
    fix_type: str
    optiscore_delta: float = 0.0
    can_auto_apply: bool = False
    tag_snippet: Optional[dict[str, str]] = None
    mutation_payload: Optional[dict[str, Any]] = None
    implementation_guide: Optional[ConversionFixGuide] = None

    def compute_fingerprint(self, campaign_id: str) -> str:
        return f"card:GOOGLE:{campaign_id}:{str(self.check_id).strip()}"


class ConversionHealthReport(CamelModel):
    """Conversion health tool output stored inside campaign recommendations."""

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
    text: str
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
    scope: str
    portfolio_strategy_id: Optional[str] = None
    portfolio_strategy_name: Optional[str] = None
    current_strategy: str
    bidding_rec_type: Optional[str] = None
    bidding_rec_rationale: Optional[str] = None
    bidding_confidence: str = "low"
    bidding_blocked_reason: Optional[str] = None
    current_budget: float = 0.0
    recommended_budget: Optional[float] = None
    budget_rec_type: Optional[str] = None
    budget_rec_rationale: Optional[str] = None
    budget_confidence: str = "low"
    apply_order: list[str] = []
    learning_phase_warning: Optional[str] = None
    constraint_type: str = "NONE"
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


# 4. Meta Ads Specific Types, Enums and Models


class MetaCampaignOverview(CamelModel):
    """Meta-specific top-level campaign overview/metadata."""

    status: str = "UNKNOWN"
    spend: float = 0.0
    impressions: int = 0
    clicks: int = 0
    conversions: float = 0.0


class MetaAgeRecommendation(WorkflowItem):
    adset_id: Optional[str] = None
    adset_name: Optional[str] = None
    action: Optional[str] = None
    current_min: Optional[int] = None
    current_max: Optional[int] = None
    recommended_min: Optional[int] = None
    recommended_max: Optional[int] = None
    recommendation: Optional[str] = Field(default="REMOVE", kw_only=True)

    def compute_fingerprint(self, campaign_id: str) -> str:
        return (
            f"age:META:{campaign_id}"
            f":{self.adset_id or ''}"
            f":{self.current_min or ''}_{self.current_max or ''}"
            f":{str(self.recommendation or '').upper()}"
        )


class MetaGenderRecommendation(WorkflowItem):
    adset_id: Optional[str] = None
    adset_name: Optional[str] = None
    action: Optional[str] = None
    recommendation: Optional[str] = Field(default="REMOVE", kw_only=True)

    def compute_fingerprint(self, campaign_id: str) -> str:
        return (
            f"gender:META:{campaign_id}"
            f":{self.adset_id or ''}"
            f":{str(self.action or '').upper()}"
            f":{str(self.recommendation or '').upper()}"
        )


class MetaOptimizationFields(BaseOptimizationFields):
    """Meta-specific campaign recommendation fields."""

    overview: Optional[MetaCampaignOverview] = None
    age: list[MetaAgeRecommendation] = []
    gender: list[MetaGenderRecommendation] = []


# 5. Top-Level Bundles, Unions and Aliases


class GoogleCampaignRecommendation(BaseCampaignRecommendation):
    """Stored Google campaign recommendation bundle."""

    platform: Literal["GOOGLE"] = "GOOGLE"
    campaign_status: CampaignStatus = CampaignStatus.UNKNOWN
    fields: GoogleOptimizationFields = Field(default_factory=GoogleOptimizationFields)


class MetaCampaignRecommendation(BaseCampaignRecommendation):
    """Stored Meta campaign recommendation bundle."""

    platform: Literal["META"] = "META"
    fields: MetaOptimizationFields = Field(default_factory=MetaOptimizationFields)


# Discriminated Union type alias for standard validation
CampaignRecommendation = Annotated[
    Union[GoogleCampaignRecommendation, MetaCampaignRecommendation],
    Field(discriminator="platform"),
]
