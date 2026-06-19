"""Platform-agnostic recommendation models — the shared core.

Leaf module (stdlib + pydantic only); everything else imports downward from
here. Holds only cross-platform primitives — platform-specific models, including
each platform's own conversion-health shape, live in their platform file.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, ClassVar, Optional
from pydantic import BaseModel, Field
from pydantic.alias_generators import to_camel


class Platform(str, Enum):
    GOOGLE = "GOOGLE"
    META = "META"

    @classmethod
    def _missing_(cls, value: Any) -> Platform:
        if not value:
            return cls.GOOGLE
        val_upper = str(value).strip().upper()
        for member in cls:
            if member.value == val_upper:
                return member
        return cls.GOOGLE

    @classmethod
    def coerce(cls, raw: Any) -> Optional[Platform]:
        """Fuzzy-match a user/storage platform label to a Platform, or None when
        unrecognized. The single source of the matching rules (handles "google
        ads", "facebook", …). Distinct from ``_missing_`` (lenient model parsing,
        which defaults to GOOGLE): ``coerce`` returns None so callers can branch on
        'not a known platform' — never silently route an unknown to Google."""
        value = str(raw or "").strip().lower()
        if "meta" in value or "facebook" in value:
            return cls.META
        if "google" in value:
            return cls.GOOGLE
        return None


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

    @classmethod
    def _missing_(cls, value: Any) -> ConfidenceLevel:
        if not value:
            return cls.LOW
        val_lower = str(value).strip().lower()
        for member in cls:
            if member.value == val_lower:
                return member
        return cls.LOW


class ScopeType(str, Enum):
    campaign = "CAMPAIGN"  # lowercase alias for mapping compatibility if parsed
    CAMPAIGN = "CAMPAIGN"
    PORTFOLIO = "PORTFOLIO"

    @classmethod
    def _missing_(cls, value: Any) -> ScopeType:
        if not value:
            return cls.CAMPAIGN
        val_upper = str(value).strip().upper()
        for member in cls:
            if member.value == val_upper:
                return member
        return cls.CAMPAIGN


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

    # Workflow state carried across runs for an unchanged fingerprint — the single
    # source the merge reads. Add a state field here and the merge picks it up.
    WORKFLOW_STATE_FIELDS: ClassVar[tuple[str, ...]] = (
        "status",
        "applied",
        "reviewed_at",
        "reviewed_by",
        "applied_at",
        "failure_reason",
    )

    def copy_workflow_state_from(self, source: "WorkflowItem") -> None:
        """Carry forward review/apply state from a same-fingerprint stored item so
        re-runs never reset a user's decisions."""
        for field in self.WORKFLOW_STATE_FIELDS:
            setattr(self, field, getattr(source, field))

    def compute_fingerprint(self, campaign_id: str) -> str:
        """Return a deterministic identity string for this item.

        Each concrete subclass overrides this. An empty string means the item
        has no stable identity and will always be treated as new on re-runs.
        """
        return ""


class BaseOptimizationFields(CamelModel):
    """Base fields class for platform-specific recommendation fields."""

    pass


class SkippedAnalysis(CamelModel):
    """An analysis section skipped for this campaign type (e.g. keywords on
    Performance Max). A plain CamelModel, not a WorkflowItem — a skip is not
    actionable."""

    section: str
    campaign_type: str
    reason: str


class BaseCampaignRecommendation(CamelModel):
    """Base stored campaign recommendation bundle."""

    id: Optional[str] = Field(None, alias="_id")
    platform: Platform
    parent_account_id: Optional[str] = ""
    account_id: Optional[str] = ""
    product_id: str = ""
    product_name: str = ""
    campaign_id: str
    campaign_name: str
    # Plain str (agnostic base — Meta has no channel type); Google models
    # default this to ChannelType.UNKNOWN, same value.
    campaign_type: str = "UNKNOWN"
    # Sections not analysed for this campaign type (driven by campaign_type);
    # refreshed each run — not a fingerprint-merged WorkflowItem list.
    skipped_analyses: list[SkippedAnalysis] = []
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
