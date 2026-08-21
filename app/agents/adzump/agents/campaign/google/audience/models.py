"""Data models for campaign audience targeting.

One signal shape across every source, because they all end up in a single ``Audience``
resource. Demographics stay separate: they are ``AudienceDimension`` entries
rather than segments, and age is a numeric range rather than a reference.
"""

from __future__ import annotations

import re
import unicodedata
from enum import Enum
from itertools import pairwise
from typing import Any, NamedTuple, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.agents.adzump.agents.campaign.google.audience.constants import (
    CUSTOM_KEYWORD_MAX_CHARS,
    CUSTOM_KEYWORD_MAX_CHARS_CJK,
    CUSTOM_KEYWORD_MAX_WORDS,
    CUSTOM_URL_MAX_CHARS,
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
        return _KIND_META[self].label

    @property
    def help(self) -> str:
        """One line for the panel. The kinds are the whole reason a pick reaches the right
        people, and the names alone do not say it."""
        return _KIND_META[self].help


class _KindMeta(NamedTuple):
    label: str
    help: str


_KIND_META: dict[SignalKind, _KindMeta] = {
    SignalKind.IN_MARKET: _KindMeta(
        "In-Market",
        "People shopping for this right now - the closest to ready to buy.",
    ),
    SignalKind.AFFINITY: _KindMeta(
        "Affinity",
        "People who are into this generally, but are not shopping yet. Reaches far more "
        "people, and fewer of them are ready.",
    ),
    SignalKind.LIFE_EVENT: _KindMeta(
        "Life Events",
        "People going through something that creates the need - moving home, getting "
        "married, having a baby.",
    ),
    SignalKind.DETAILED_DEMOGRAPHIC: _KindMeta(
        "Detailed Demographics",
        "Facts that stay true for years - whether they own a home, how far they studied, "
        "what they do for a living.",
    ),
    SignalKind.CUSTOM_AUDIENCE: _KindMeta(
        "Custom Segments",
        "Built for this campaign from what people type into Google, for when none of "
        "Google's ready-made segments fit.",
    ),
    SignalKind.USER_LIST: _KindMeta(
        "Your Data",
        "Lists you uploaded yourself - your customers, or people who visited your site.",
    ),
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
    """Percentiles of household income in the target country, not currency amounts.

    Each row carries the API value and the band's own edges, counting down from the richest.
    Both are needed because the API counts from the BOTTOM - 90_UP is the top 10%.
    """

    def __new__(cls, value: str, top_from: int, top_to: int) -> Self:
        band = str.__new__(cls, value)
        band._value_ = value
        band.top_from, band.top_to = top_from, top_to
        return band

    top_from: int
    top_to: int

    TOP_10 = ("INCOME_RANGE_90_UP", 0, 10)
    TOP_10_20 = ("INCOME_RANGE_80_90", 10, 20)
    TOP_20_30 = ("INCOME_RANGE_70_80", 20, 30)
    TOP_30_40 = ("INCOME_RANGE_60_70", 30, 40)
    TOP_40_50 = ("INCOME_RANGE_50_60", 40, 50)
    LOWER_50 = ("INCOME_RANGE_0_50", 50, 100)

    @property
    def label(self) -> str:
        return span_label([self])


def span_label(bands: list[IncomeRange]) -> str:
    """One unbroken run of bands named by its outer edges, as Google's picker shows it.

    Joining the end labels instead would read "Top 10% to Top 10-20%" - two overlapping
    things rather than the single 0-20% slice it actually is.
    """
    if not bands:
        return ""
    low, high = bands[0].top_from, bands[-1].top_to
    # 100 is the bottom of the ladder: a span reaching it has no upper edge left to name.
    if high == 100:
        return "Everyone" if low == 0 else f"Lower {100 - low}%"
    return f"Top {high}%" if low == 0 else f"Top {low}-{high}%"


class ParentalStatus(str, Enum):
    PARENT = "PARENT"
    NOT_A_PARENT = "NOT_A_PARENT"


# Each enum also defines UNDETERMINED, deliberately absent here: every dimension carries its
# own include_undetermined flag, and two ways to say the same thing invites contradiction.

# The four AudienceDimension slots, in the order the panel shows them.
DIMENSION_FIELDS = ("age_ranges", "genders", "income_ranges", "parental_statuses")

# common/audiences.proto, AgeSegment: "A minimum age must be specified and must be at least
# 18. Allowed values are 18, 25, 35, 45, 55, and 65." / "max_age must be greater than
# min_age, and allowed values are 24, 34, 44, 54, and 64."
MIN_AGES = (18, 25, 35, 45, 55, 65)
MAX_AGES = (24, 34, 44, 54, 64)


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
        if v not in MIN_AGES:
            raise ValueError(f"min_age must be one of {MIN_AGES}")
        return v

    @field_validator("max_age")
    @classmethod
    def _valid_max(cls, v: int | None) -> int | None:
        if v is not None and v not in MAX_AGES:
            raise ValueError(f"max_age must be one of {MAX_AGES}")
        return v

    @model_validator(mode="after")
    def _ordered(self) -> AgeRange:
        if self.max_age is not None and self.max_age <= self.min_age:
            raise ValueError("max_age must be greater than min_age")
        return self


class DemographicSpec(BaseModel):
    """AudienceDimension entries, which AND with the segments rather than joining them."""

    age_ranges: list[AgeRange] = Field(default_factory=list)
    genders: list[Gender] = Field(default_factory=list)
    income_ranges: list[IncomeRange] = Field(default_factory=list)
    parental_statuses: list[ParentalStatus] = Field(default_factory=list)
    # Per dimension, keyed like `rationales`: each *Dimension message declares its own.
    # A missing key means Google's default, ON.
    include_undetermined: dict[str, bool] = Field(default_factory=dict)
    # Why each dimension is set the way it is, keyed by the field above. Leaving one open is
    # a decision too - without the reason the panel can only show "Everyone", which reads as
    # "not considered" rather than "considered and deliberately not narrowed".
    rationales: dict[str, str] = Field(default_factory=dict)

    @field_validator("include_undetermined")
    @classmethod
    def _known_dimensions(cls, v: dict[str, bool]) -> dict[str, bool]:
        # A typo'd key reads as "left at the default" - silently the opposite.
        if unknown := sorted(set(v) - set(DIMENSION_FIELDS)):
            raise ValueError(
                f"include_undetermined: unknown dimension {unknown[0]!r}; "
                f"keys are {', '.join(DIMENSION_FIELDS)}"
            )
        return v

    @field_validator("income_ranges")
    @classmethod
    def _income_is_one_span(cls, v: list[IncomeRange]) -> list[IncomeRange]:
        """One unbroken run of bands. The API takes any set, but Google's picker is a
        from/to pair - a gap in the middle is a campaign the user cannot verify there."""
        if not v:
            return v
        order = list(IncomeRange)
        chosen = sorted({order.index(i) for i in v})
        if gaps := [i for i in range(chosen[0], chosen[-1]) if i not in chosen]:
            raise ValueError(
                f"income ranges must be one unbroken span - {order[gaps[0]].label} is "
                "missing from the middle"
            )
        return [order[i] for i in chosen]

    def includes_undetermined(self, field: str) -> bool:
        """Defaults ON, as every "Unknown" box in Google's UI starts checked. Off is a real
        narrowing - for income, undetermined is most users outside the reported countries."""
        return self.include_undetermined.get(field, True)

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


def _display_width_limit(text: str) -> int:
    """Google counts double-width characters against a halved cap.

    east_asian_width W (wide) and F (fullwidth) are the double-width classes; one of them
    anywhere in the term switches the whole term to the CJK limit.
    """
    wide = any(unicodedata.east_asian_width(c) in ("W", "F") for c in text)
    return CUSTOM_KEYWORD_MAX_CHARS_CJK if wide else CUSTOM_KEYWORD_MAX_CHARS


class CustomSegmentTerm(BaseModel):
    """One keyword member of a custom segment.

    Google's limits are enforced HERE because the API will not: validateOnly on
    customAudiences:mutate accepts an 11-word, 81-character keyword (probed live). Nothing
    downstream would report it, and the created segment would not target what we meant.
    """

    keyword: str
    volume: int = 0

    @field_validator("keyword")
    @classmethod
    def _within_googles_limits(cls, v: str) -> str:
        term = " ".join((v or "").split())  # collapse whitespace before measuring
        if not term:
            raise ValueError("a term cannot be empty")
        if len(term.split()) > CUSTOM_KEYWORD_MAX_WORDS:
            raise ValueError(f"'{term}' is over {CUSTOM_KEYWORD_MAX_WORDS} words")
        limit = _display_width_limit(term)
        if len(term) > limit:
            raise ValueError(f"'{term[:40]}...' is over {limit} characters")
        return term


# Two or more dot-separated segments, each starting with a letter. Matches Android's own
# rule closely enough to catch a pasted App Store id or a bare app name.
_ANDROID_PACKAGE = re.compile(r"[a-zA-Z][\w]*(\.[a-zA-Z][\w]*)+")


class CustomSegmentUrl(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def _within_googles_limits(cls, v: str) -> str:
        url = (v or "").strip()
        if not url.startswith(("http://", "https://")):
            # proto: "An HTTP URL, protocol-included" - a bare domain is silently useless.
            raise ValueError(f"'{url}' must include http:// or https://")
        if len(url) > CUSTOM_URL_MAX_CHARS:
            raise ValueError(f"url is over {CUSTOM_URL_MAX_CHARS} characters")
        return url


class CustomSegmentApp(BaseModel):
    """An APP member. The proto documents an ANDROID package name; nothing states how an iOS
    app is expressed, so only Android is accepted rather than guessing a shape."""

    app: str

    @field_validator("app")
    @classmethod
    def _looks_like_a_package(cls, v: str) -> str:
        app = (v or "").strip()
        if not _ANDROID_PACKAGE.fullmatch(app):
            raise ValueError(
                f"'{app}' is not an Android package name - it looks like com.example.app, "
                "and you can copy it from the app's Play Store link (the id= part)"
            )
        return app


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
