from typing import Literal, Optional, List, Dict, Any
from pydantic import BaseModel, Field


class BaseCampaignRecommendation(BaseModel):
    """Shared contract for all platform campaign recommendations.
    Both Google and Meta campaign recommendation models inherit from this.
    """

    id: Optional[str] = Field(None, alias="_id")
    platform: str  # "GOOGLE" | "META"
    parent_account_id: str = Field(
        ..., min_length=1
    )  # Google: loginCustomerId, Meta: businessId
    account_id: str = Field(..., min_length=1)  # Google: customerId, Meta: adAccountId
    product_id: Optional[str] = None  # Linked product / website
    campaign_id: str = Field(..., min_length=1)
    campaign_name: str
    campaign_type: str  # Google: "SEARCH"/"DISPLAY", Meta: objective string
    completed: bool = False


class MetaAgeFieldRecommendation(BaseModel):
    adset_id: str
    adset_name: str
    current_min: int
    current_max: int
    recommended_min: int
    recommended_max: int
    action: str = "UPDATE_AGE_RANGE"
    reason: str
    applied: bool = False


class MetaOptimizationFields(BaseModel):
    age: Optional[List[MetaAgeFieldRecommendation]] = None


class MetaCampaignRecommendation(BaseCampaignRecommendation):
    """Meta Ads campaign recommendation."""

    platform: Literal["META"] = "META"
    fields: MetaOptimizationFields


class MetaOptimizationResponse(BaseModel):
    success: bool
    message: str
    recommendations: List[MetaCampaignRecommendation] = []
    errors: List[Dict[str, Any]] = []


class MetaAgeAIRecommendation(BaseModel):
    adset_id: str
    recommended_age_min: int
    recommended_age_max: int
    reason: str


class MetaAgeAIResponse(BaseModel):
    """The strict schema the AI must follow for recommendations."""
    recommendations: List[MetaAgeAIRecommendation]
