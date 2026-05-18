from typing import Any
from pydantic import BaseModel, Field


KEYWORD_MAX_LENGTH = 80


class SearchTermMetrics(BaseModel):
    impressions: int = 0
    clicks: int = 0
    conversions: float = 0
    cost: float = 0
    ctr: float = 0
    average_cpc: float = 0
    cost_per_conversion: float = 0


class BrandAnalysis(BaseModel):
    match: bool = False
    type: str = "generic"
    competitor_detected: bool = False
    match_level: str = "No Match"
    reason: str = ""


class ConfigurationAnalysis(BaseModel):
    match: bool = False
    score: float = 0.0
    match_level: str = "No Match"
    reason: str = ""


class LocationAnalysis(BaseModel):
    match: bool = False
    match_level: str = "No Match"
    reason: str = ""


class SearchTermAnalysis(BaseModel):
    brand: BrandAnalysis = Field(default_factory=BrandAnalysis)
    configuration: ConfigurationAnalysis = Field(default_factory=ConfigurationAnalysis)
    location: LocationAnalysis = Field(default_factory=LocationAnalysis)
    strength: str = "LOW"


class KeywordRecommendation(BaseModel):
    text: str
    match_type: str

    reason: str

    metrics: SearchTermMetrics
    analysis: SearchTermAnalysis

    ad_group_id: str | None = None
    ad_group_name: str | None = None

    criterion_id: str | None = None
    resource_name: str | None = None


class OptimizationFields(BaseModel):
    """
    Generic container for all types of optimization recommendations.
    Add fields here as new optimization types are supported.
    """
    keywords: list[KeywordRecommendation] | None = None
    negativeKeywords: list[KeywordRecommendation] | None = None
    
    # Placeholder for future optimizations
    budget_recommendation: Any | None = None
    bid_recommendation: Any | None = None


class CampaignRecommendation(BaseModel):
    _id: str | None = None

    platform: str

    parent_account_id: str
    account_id: str

    product_id: str

    campaign_id: str
    campaign_name: str
    campaign_type: str

    completed: bool = False

    fields: OptimizationFields = Field(default_factory=OptimizationFields)


class SearchTermEvaluation(BaseModel):
    recommendation_type: str
    text: str
    reason: str

    metrics: SearchTermMetrics
    analysis: SearchTermAnalysis
