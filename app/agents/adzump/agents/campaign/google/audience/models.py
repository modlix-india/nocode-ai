"""Data models for campaign audience targeting.

One signal shape across every source, because they all end up in a single ``Audience``
resource. Demographics stay separate: they are ``AudienceDimension`` entries
rather than segments, and age is a numeric range rather than a reference.
"""

from __future__ import annotations

from enum import Enum
from itertools import pairwise
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.agents.adzump.agents.campaign.google.audience.constants import (
    MAX_SIGNALS_PER_KIND,
)


class SignalKind(str, Enum):
    """Split by what a segment MEANS, not by which resource it came from.

    IN_MARKET and AFFINITY are both ``user_interest`` and emit identically, but they target
    different people - "buying this" vs "into this" - and the panel groups on that. Merging
    them into one kind loses the distinction the user needs to choose correctly.
    """

    IN_MARKET = "IN_MARKET"  # what they are buying
    AFFINITY = "AFFINITY"  # what they are into
    LIFE_EVENT = "LIFE_EVENT"  # what is happening to them
    DETAILED_DEMOGRAPHIC = "DETAILED_DEMOGRAPHIC"  # who they are, what they do
    CUSTOM_AUDIENCE = "CUSTOM_AUDIENCE"  # signals we described
    USER_LIST = "USER_LIST"  # the advertiser's own data

    @property
    def label(self) -> str:
        return _KIND_LABELS[self]


_KIND_LABELS = {
    SignalKind.IN_MARKET: "In-Market",
    SignalKind.AFFINITY: "Affinity",
    SignalKind.LIFE_EVENT: "Life Events",
    SignalKind.DETAILED_DEMOGRAPHIC: "Detailed Demographics",
    SignalKind.CUSTOM_AUDIENCE: "Custom Segments",
    SignalKind.USER_LIST: "Your Data",
}

# custom_affinity, custom_intent and combined_audience are deliberately absent: they exist on
# AdGroupCriterion but have no AudienceSegment equivalent, so grouped mode cannot express them.


class SignalSource(str, Enum):
    TAXONOMY = "TAXONOMY"  # ranked out of Google's cached taxonomy
    GENERATED = "GENERATED"  # we described it (custom audience)
    ACCOUNT = "ACCOUNT"  # already existed in the advertiser's account


class SignalMetrics(BaseModel):
    """Reach numbers. Always None today - AudienceInsightsService is allowlisted behind a
    data-licensing agreement, so nothing populates this. It exists so that if access ever
    lands, the panel and models need no change."""

    reach_low: int | None = None
    reach_high: int | None = None


class AudienceSignal(BaseModel):
    kind: SignalKind
    ref: str  # what the API needs: a resource name, or an id for life events
    label: str  # what the user sees
    source: SignalSource
    rationale: str = ""
    path: list[str] = Field(default_factory=list)  # ancestors, for panel display
    negative: bool = False
    owned: bool = False  # created by us, matched on OWNED_MARKER
    metrics: SignalMetrics | None = None

    @field_validator("ref", "label")
    @classmethod
    def _required(cls, v: str) -> str:
        text = (v or "").strip()
        if not text:
            raise ValueError("ref and label cannot be empty")
        return text

    @model_validator(mode="after")
    def _only_user_lists_exclude(self) -> AudienceSignal:
        # ExclusionSegment has a single variant, user_list. Excluding an interest is not
        # expressible, and accepting it here would drop it silently at emit.
        if self.negative and self.kind is not SignalKind.USER_LIST:
            raise ValueError(
                f"{self.kind.value} cannot be excluded - only user lists can"
            )
        return self


# Values below are transcribed from the v23 enum protos, not inferred:
# enums/gender_type.proto · enums/income_range_type.proto · enums/parental_status_type.proto
# https://github.com/googleapis/googleapis/tree/master/google/ads/googleads/v23/enums


class Gender(str, Enum):
    MALE = "MALE"
    FEMALE = "FEMALE"


class IncomeRange(str, Enum):
    """Percentiles of household income in the target country, not currency amounts — the
    proto comments read "0%-50%", "80% to 90%", "Greater than 90%". A premium product wants
    the top bands, not a rupee figure."""

    TOP_10 = "INCOME_RANGE_90_UP"
    TOP_10_20 = "INCOME_RANGE_80_90"
    TOP_20_30 = "INCOME_RANGE_70_80"
    TOP_30_40 = "INCOME_RANGE_60_70"
    TOP_40_50 = "INCOME_RANGE_50_60"
    LOWER_50 = "INCOME_RANGE_0_50"

    @property
    def label(self) -> str:
        """The band as a person reads it. The API value names its bounds
        ("INCOME_RANGE_90_UP"), which shown verbatim reads as an income, not a percentile."""
        return _INCOME_LABELS[self]


_INCOME_LABELS = {
    IncomeRange.TOP_10: "Top 10%",
    IncomeRange.TOP_10_20: "Top 10-20%",
    IncomeRange.TOP_20_30: "Top 20-30%",
    IncomeRange.TOP_30_40: "Top 30-40%",
    IncomeRange.TOP_40_50: "Top 40-50%",
    IncomeRange.LOWER_50: "Lower 50%",
}


class ParentalStatus(str, Enum):
    PARENT = "PARENT"
    NOT_A_PARENT = "NOT_A_PARENT"


# Each enum also defines UNDETERMINED, deliberately absent here: every dimension carries its
# own include_undetermined flag, and two ways to say the same thing invites contradiction.

# common/audiences.proto, AgeSegment: "A minimum age must be specified and must be at least
# 18. Allowed values are 18, 25, 35, 45, 55, and 65." / "max_age must be greater than
# min_age, and allowed values are 24, 34, 44, 54, and 64."
_MIN_AGES = (18, 25, 35, 45, 55, 65)
_MAX_AGES = (24, 34, 44, 54, 64)


class AgeRange(BaseModel):
    """AgeSegment takes integers, not the AgeRangeType enum AdGroupCriterion uses.

    One range may span several of Google's bands - 25 to 54 is a single valid segment, not
    three - so only the endpoints are constrained.
    """

    min_age: int
    max_age: int | None = None

    @field_validator("min_age")
    @classmethod
    def _valid_min(cls, v: int) -> int:
        if v not in _MIN_AGES:
            raise ValueError(f"min_age must be one of {_MIN_AGES}")
        return v

    @field_validator("max_age")
    @classmethod
    def _valid_max(cls, v: int | None) -> int | None:
        if v is not None and v not in _MAX_AGES:
            raise ValueError(f"max_age must be one of {_MAX_AGES}")
        return v

    @model_validator(mode="after")
    def _ordered(self) -> AgeRange:
        if self.max_age is not None and self.max_age <= self.min_age:
            raise ValueError("max_age must be greater than min_age")
        return self


class DemographicSpec(BaseModel):
    """AudienceDimension entries, which AND with the segments rather than joining them.

    include_undetermined defaults true: Google cannot determine age or gender for a large
    share of users, and leaving it false silently discards them.
    """

    age_ranges: list[AgeRange] = Field(default_factory=list)
    genders: list[Gender] = Field(default_factory=list)
    income_ranges: list[IncomeRange] = Field(default_factory=list)
    parental_statuses: list[ParentalStatus] = Field(default_factory=list)
    include_undetermined: bool = True

    @model_validator(mode="after")
    def _age_ranges_do_not_overlap(self) -> DemographicSpec:
        """Each AgeSegment is one contiguous span, and a person sits in exactly one of them.

        Overlapping spans say nothing extra — 18-34 plus 25-44 is just 18-44 — so they are a
        model's mistake rather than an intent, and a range spanning several bands makes them
        easy to produce by accident.
        """
        spans = sorted(
            (r.min_age, r.max_age if r.max_age is not None else 200)
            for r in self.age_ranges
        )
        for (lo, hi), (next_lo, _) in pairwise(spans):
            if next_lo <= hi:
                raise ValueError(
                    f"age ranges overlap: {lo}-{hi} and {next_lo}-; merge them into one"
                )
        return self

    @property
    def is_empty(self) -> bool:
        return not (
            self.age_ranges
            or self.genders
            or self.income_ranges
            or self.parental_statuses
        )


class AudienceTargetingResult(BaseModel):
    """The agent's output: one ad group, one Audience resource."""

    signals: list[AudienceSignal] = Field(default_factory=list)
    demographics: DemographicSpec = Field(default_factory=DemographicSpec)
    # Segment refs grouped into AudienceSegmentDimensions: AND across groups, OR within.
    # Default is one group holding every positive - the broad shape these campaigns want.
    dimension_groups: list[list[str]] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _no_duplicate_refs(self) -> AudienceTargetingResult:
        seen: set[str] = set()
        for s in self.signals:
            if s.ref in seen:
                raise ValueError(f"duplicate signal ref: {s.ref}")
            seen.add(s.ref)
        return self

    @model_validator(mode="after")
    def _groups_partition_the_positives(self) -> AudienceTargetingResult:
        """Each group becomes an AudienceSegmentDimension, and dimensions AND together.

        A ref in no group is silently not targeted; a ref in two lands in two ANDed
        dimensions and narrows the audience to their intersection. Both produce a valid
        request and a wrong campaign, so neither can be left to the emitter to notice.
        """
        if not self.dimension_groups:
            return self
        grouped = [ref for group in self.dimension_groups for ref in group]
        duplicates = {r for r in grouped if grouped.count(r) > 1}
        if duplicates:
            raise ValueError(
                f"ref in more than one dimension group: {sorted(duplicates)}"
            )
        positives = {s.ref for s in self.positives}
        unknown = set(grouped) - positives
        if unknown:
            raise ValueError(
                f"dimension group references unknown ref: {sorted(unknown)}"
            )
        ungrouped = positives - set(grouped)
        if ungrouped:
            raise ValueError(
                f"positive not in any dimension group: {sorted(ungrouped)}"
            )
        return self

    @property
    def positives(self) -> list[AudienceSignal]:
        return [s for s in self.signals if not s.negative]

    def over_cap(self) -> dict[SignalKind, int]:
        """Kinds exceeding the per-kind guard, with their counts."""
        counts: dict[SignalKind, int] = {}
        for s in self.positives:
            counts[s.kind] = counts.get(s.kind, 0) + 1
        return {k: n for k, n in counts.items() if n > MAX_SIGNALS_PER_KIND}
