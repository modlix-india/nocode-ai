from enum import Enum
from pydantic import BaseModel, Field


class TargetingCategory(str, Enum):
    """Targeting categories supported by Meta Graph API."""

    INTERESTS = "interests"
    DEMOGRAPHICS = "demographics"
    BEHAVIORS = "behaviors"


def map_type_to_category(segment_type: str) -> str:
    """Map granular Meta targeting types to frontend category buckets."""
    t = str(segment_type).lower()
    if t in ("interests", "interest", "adinterest"):
        return TargetingCategory.INTERESTS.value
    if t in ("behaviors", "behavior"):
        return TargetingCategory.BEHAVIORS.value
    return TargetingCategory.DEMOGRAPHICS.value


class TargetingEntity(BaseModel):
    """Individual Meta targeting segment details."""

    id: str
    name: str = ""
    type: str = ""
    audience_size_lower_bound: int | None = None
    audience_size_upper_bound: int | None = None
    path: list[str] = Field(default_factory=list)
    description: str = ""
    category: str | None = None


class MetaTargetingSuggestionResult(BaseModel):
    """Final targeting suggestions output grouped by category."""

    interests: list[TargetingEntity] = Field(default_factory=list)
    demographics: list[TargetingEntity] = Field(default_factory=list)
    behaviors: list[TargetingEntity] = Field(default_factory=list)
