"""Meta Ads recommendation models. Imports downward from base.py only."""
from __future__ import annotations

from typing import Optional, Literal
from pydantic import Field

from app.agents.adzump.recommendations.models.base import (
    CamelModel,
    WorkflowItem,
    BaseOptimizationFields,
    BaseCampaignRecommendation,
    Platform,
)


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


class MetaCampaignRecommendation(BaseCampaignRecommendation):
    """Stored Meta campaign recommendation bundle."""

    platform: Literal[Platform.META] = Platform.META
    fields: MetaOptimizationFields = Field(default_factory=MetaOptimizationFields)
