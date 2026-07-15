"""Data models for keyword research.

The submit tools build OptimizedKeyword / NegativeKeyword from the LLM's raw output,
so the validators here (normalisation, length, match-type/intent coercion,
cross-business -> phrase) run on every keyword that reaches the campaign. The tools add
the cross-item checks (candidate membership, overlap, safety) the models can't see.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, Field, field_validator, model_validator

from app.agents.adzump.adapters.autosuggest import DEFAULT_SOURCE_NAMES
from app.agents.adzump.agents.campaign.google.keyword.themes import get_theme
from app.agents.adzump.agents.campaign.google.keyword.constants import (
    COMMERCIAL_VALUE_COMPETITION_LEVELS,
    INFORMATIONAL_SOURCE_NAMES,
    KEYWORD_MAX_LENGTH,
    KEYWORD_MAX_WORDS,
    KEYWORD_MIN_LENGTH,
)


def normalize(value: str) -> str:
    return " ".join(str(value or "").split()).lower()


class MatchType(str, Enum):
    # Google Ads KeywordMatchType values (used verbatim at campaign creation).
    EXACT = "EXACT"
    PHRASE = "PHRASE"
    BROAD = "BROAD"


class CompetitionLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class Intent(str, Enum):
    INFORMATIONAL = "informational"
    NAVIGATIONAL = "navigational"
    COMMERCIAL = "commercial"
    TRANSACTIONAL = "transactional"
    UNKNOWN = "unknown"


def _coerce_positive_match_type(v: object) -> str:
    # Positives target: exact or phrase, never broad (broad positives over-spend).
    s = str(v or "").upper()
    return s if s in ("EXACT", "PHRASE") else "PHRASE"


def _coerce_negative_match_type(v: object) -> str:
    # Negatives exclude a concept: phrase or broad. Exact blocks only the literal
    # query (every variation leaks through), so it's coerced up to phrase.
    s = str(v or "").upper()
    return s if s in ("PHRASE", "BROAD") else "PHRASE"


def _coerce_intent(v: object) -> str:
    s = str(v or "").lower()
    return s if s in {i.value for i in Intent} else "unknown"


# An LLM label is coerced to a valid enum rather than raising, so a stray value never
# discards an otherwise-good keyword. Positives and negatives allow different sets.
CoercedPositiveMatchType = Annotated[MatchType, BeforeValidator(_coerce_positive_match_type)]
CoercedNegativeMatchType = Annotated[MatchType, BeforeValidator(_coerce_negative_match_type)]
CoercedIntent = Annotated[Intent, BeforeValidator(_coerce_intent)]


class KeywordSuggestion(BaseModel):
    """A Planner-scored candidate: real Google volume, competition, CPC."""

    keyword: str = Field(
        ..., min_length=KEYWORD_MIN_LENGTH, max_length=KEYWORD_MAX_LENGTH
    )
    volume: int = Field(default=0, ge=0)
    competition: CompetitionLevel = CompetitionLevel.UNKNOWN
    competition_index: float = Field(default=0.0, ge=0.0, le=1.0)
    cpc_low: float = Field(default=0.0, ge=0.0)
    cpc_high: float = Field(default=0.0, ge=0.0)
    intent: CoercedIntent = Intent.UNKNOWN  # assigned by the selection LLM
    source: str = ""  # surface that first surfaced it

    @field_validator("keyword")
    @classmethod
    def _norm_keyword(cls, v: str) -> str:
        text = normalize(v)
        # Google Ads rejects >10 words (char limit is the Field max_length); both apply.
        if len(text.split()) > KEYWORD_MAX_WORDS:
            raise ValueError(f"keyword exceeds {KEYWORD_MAX_WORDS} words")
        return text

    @property
    def roi_score(self) -> float:
        return (
            self.volume / (1 + self.competition_index)
            if self.competition_index
            else float(self.volume)
        )

    @property
    def commercial_value(self) -> float:
        # Google's own 0..1 measure of advertiser demand is the commercial signal.
        return self.competition_index

    @property
    def has_ad_market(self) -> bool:
        # Advertisers compete/bid on it -> worth a paid Search campaign.
        return (
            self.competition in COMMERCIAL_VALUE_COMPETITION_LEVELS
            or self.competition_index > 0.0
            or self.cpc_high > 0.0
        )


class OptimizedKeyword(KeywordSuggestion):
    """A selected positive with its match type + rationale."""

    match_type: CoercedPositiveMatchType = MatchType.PHRASE
    rationale: str = ""
    is_cross_business: bool = False

    @model_validator(mode="after")
    def _phrase_for_cross_business(self) -> "OptimizedKeyword":
        if self.is_cross_business and self.match_type is not MatchType.PHRASE:
            self.match_type = MatchType.PHRASE  # exact would over-target a wrong cohort
        return self


class NegativeKeyword(BaseModel):
    keyword: str = Field(
        ..., min_length=KEYWORD_MIN_LENGTH, max_length=KEYWORD_MAX_LENGTH
    )
    reason: str = ""
    volume: int = Field(default=0, ge=0)  # shown in the panel; via historical metrics
    theme: str = ""  # the theme this excludes for
    match_type: CoercedNegativeMatchType = MatchType.PHRASE

    @field_validator("keyword")
    @classmethod
    def _norm_keyword(cls, v: str) -> str:
        text = normalize(v)
        # Google Ads rejects >10 words (char limit is the Field max_length); both apply.
        if len(text.split()) > KEYWORD_MAX_WORDS:
            raise ValueError(f"keyword exceeds {KEYWORD_MAX_WORDS} words")
        return text


class Rejection(BaseModel):
    """A candidate that did NOT make the set — the record behind "why not this keyword?"."""

    keyword: str
    rule: str  # not_selected | zero_volume | overlaps_positive | unsafe | duplicate | invented
    volume_at_eval: int = Field(default=0, ge=0)
    reason: str = ""  # the model's words, when it named one


class KeywordSet(BaseModel):
    """One theme's ad group: what we target, what we exclude, and what we passed over."""

    theme: str  # KeywordTheme.id
    label: str = ""  # carried so the panel renders without a code-side lookup
    positives: list[OptimizedKeyword] = Field(default_factory=list)
    negatives: list[NegativeKeyword] = Field(default_factory=list)
    rejections: list[Rejection] = Field(default_factory=list)


class KeywordResearchResult(BaseModel):
    """One ad group per chosen theme — never merged."""

    themes: dict[str, KeywordSet] = Field(default_factory=dict)  # theme id -> its set
    meta: dict = Field(default_factory=dict)  # geo, key, failed — stays at the root


class OfferingTaxonomy(BaseModel):
    """Offering boundary derived from product_data.
    Anchors seed/positive keywords and defines negative siblings
    without hardcoded category lists."""

    primary_offering: str = (
        ""  # crisp category a buyer shops for, e.g. "duplex villaments"
    )
    core_terms: list[str] = Field(
        default_factory=list
    )  # what they sell — anchor positives here
    sibling_categories: list[str] = Field(
        default_factory=list
    )  # same industry, NOT sold — the negative boundary
    is_location_specific: bool = (
        True  # LLM-decided: serves specific areas (local/regional) vs national/online
    )
    includes_informational_funnel: bool = (
        False  # buyers research via how-to/educational content → adds YouTube autosuggest
    )
    complete: bool = (
        True  # False = transient fail-soft fallback (the caller must not cache it)
    )

    @field_validator("core_terms", "sibling_categories")
    @classmethod
    def _norm_terms(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for term in v:
            n = normalize(str(term))
            if n and n not in seen:
                seen.add(n)
                out.append(n)
        return out


@dataclass(frozen=True)
class BusinessProfile:
    """Category + the autosuggest-source signals for one run (both from the taxonomy step)."""

    category: str
    includes_informational_funnel: bool = False  # → YouTube informational autosuggest

    def source_names(self, theme_id: str) -> list[str]:
        """Autosuggest surfaces for one theme. The web-search defaults are always the
        primary set; informational surfaces need BOTH the theme to allow them and the
        business to actually have that theme."""
        names = list(DEFAULT_SOURCE_NAMES)
        if get_theme(theme_id).allows_informational_sources and self.includes_informational_funnel:
            names += [n for n in INFORMATIONAL_SOURCE_NAMES if n not in names]
        return names
