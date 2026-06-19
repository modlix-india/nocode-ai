"""Recommendation models — public package surface.

Import sites use ``from app.agents.adzump.recommendations.models import X``.
Re-exports base/google/meta and defines the discriminated CampaignRecommendation union.
"""
from __future__ import annotations

from typing import Annotated, Union
from pydantic import Field

from app.agents.adzump.recommendations.models.base import (
    Platform,
    RecommendationStatus,
    CheckSeverity,
    ConfidenceLevel,
    ScopeType,
    CamelModel,
    WorkflowItem,
    BaseOptimizationFields,
    BaseCampaignRecommendation,
    SkippedAnalysis,
)
from app.agents.adzump.recommendations.models.google import (
    HealthLabel,
    AgeRangeType,
    GenderType,
    ChannelType,
    CampaignStatus,
    ConversionSignalStatus,
    ConstraintType,
    MatchType,
    BiddingStrategyType,
    ConversionSignal,
    ConversionHealthCheck,
    ConversionFixStep,
    ConversionFixGuide,
    ConversionFixCard,
    ConversionHealthReport,
    GoogleKeywordRecommendation,
    BudgetBiddingRecommendation,
    GoogleAgeRecommendation,
    GoogleGenderRecommendation,
    CampaignOverview,
    GoogleOptimizationFields,
    GoogleCampaignRecommendation,
)
from app.agents.adzump.recommendations.models.meta import (
    MetaCampaignOverview,
    MetaAgeRecommendation,
    MetaGenderRecommendation,
    MetaOptimizationFields,
    MetaCampaignRecommendation,
)

CampaignRecommendation = Annotated[
    Union[GoogleCampaignRecommendation, MetaCampaignRecommendation],
    Field(discriminator="platform"),
]

__all__ = [
    "Platform",
    "RecommendationStatus",
    "CheckSeverity",
    "ConfidenceLevel",
    "ScopeType",
    "CamelModel",
    "WorkflowItem",
    "BaseOptimizationFields",
    "BaseCampaignRecommendation",
    "SkippedAnalysis",
    "HealthLabel",
    "AgeRangeType",
    "GenderType",
    "ChannelType",
    "CampaignStatus",
    "ConversionSignalStatus",
    "ConstraintType",
    "MatchType",
    "BiddingStrategyType",
    "ConversionSignal",
    "ConversionHealthCheck",
    "ConversionFixStep",
    "ConversionFixGuide",
    "ConversionFixCard",
    "ConversionHealthReport",
    "GoogleKeywordRecommendation",
    "BudgetBiddingRecommendation",
    "GoogleAgeRecommendation",
    "GoogleGenderRecommendation",
    "CampaignOverview",
    "GoogleOptimizationFields",
    "GoogleCampaignRecommendation",
    "MetaCampaignOverview",
    "MetaAgeRecommendation",
    "MetaGenderRecommendation",
    "MetaOptimizationFields",
    "MetaCampaignRecommendation",
    "CampaignRecommendation",
]
