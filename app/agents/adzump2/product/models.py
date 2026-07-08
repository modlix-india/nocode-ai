"""Structured contract for A2 (product study) — see ``A2-product-study.md`` §5.1.

All models are plain dataclasses with ``to_dict()`` so a ``ProductStudyResult``
rides the session context (JSON-serialized) and the J9 profile write-back
without a Pydantic dependency.

``AssetGaps`` is re-declared here (same shape + interface as the legacy
``adzump/agents/product/models.AssetGaps``) rather than imported: the legacy
module evaluates ``X | None`` annotations at class-definition time, so it will
not import on Python 3.9 (the venv). The orchestrator normalizes whatever the
reused ProductAgent returns via ``AssetGaps.from_dict(gaps.to_dict())`` — both
classes expose the same dict shape, so it round-trips regardless of which one
produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Competitor sources (CONTRACT / A2 §5.1). Uppercase enum, no silent fallback.
COMPETITOR_SOURCES: tuple[str, ...] = ("MAPS", "WEB", "AD_LIBRARY", "LLM")


@dataclass
class AssetGaps:
    """Assets the site couldn't supply → the user is asked to upload them.

    Mirrors the legacy ``AssetGaps`` (logo + missing creative categories +
    launch-readiness verdict). JSON-safe; never store a live instance without
    ``to_dict()`` on the persisted context.
    """

    logo_missing: bool = False
    missing_categories: list[str] = field(default_factory=list)
    verdict: str = ""

    def any_open(self) -> bool:
        return self.logo_missing or bool(self.missing_categories)

    def to_dict(self) -> dict[str, Any]:
        return {
            "logo_missing": self.logo_missing,
            "missing_categories": list(self.missing_categories),
            "verdict": self.verdict,
        }

    @classmethod
    def from_dict(cls, d: Any) -> "AssetGaps":
        if not isinstance(d, dict):
            return cls()
        return cls(
            logo_missing=bool(d.get("logo_missing")),
            missing_categories=list(d.get("missing_categories") or []),
            verdict=str(d.get("verdict") or ""),
        )

    @classmethod
    def coerce(cls, obj: Any) -> "AssetGaps":
        """Normalize anything AssetGaps-shaped (incl. the legacy dataclass) via
        its ``to_dict()``; ``None`` → an empty gaps object."""
        if obj is None:
            return cls()
        if isinstance(obj, cls):
            return obj
        to_dict = getattr(obj, "to_dict", None)
        if callable(to_dict):
            return cls.from_dict(to_dict())
        return cls.from_dict(obj)


@dataclass
class VerticalGuess:
    """The deduced J5 vertical code + the classifier's confidence + rationale.

    ``code`` is a J5 registry code (``real_estate``) or ``generic`` when the
    classifier is unsure — the effective code selects the whole downstream
    playbook (A2 §5.3).
    """

    code: str
    confidence: float
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "confidence": round(float(self.confidence), 4),
            "rationale": self.rationale,
        }


@dataclass
class Competitor:
    """One discovered competitor (A2 §5.1).

    Exactly one of ``url`` / ``page_id`` locates it; ``source`` says how it was
    found (``MAPS`` places, ``WEB`` search+fetch, ``AD_LIBRARY`` category, or an
    ``LLM`` shortlist); ``confidence`` is 0..1.
    """

    name: str
    source: str = "LLM"
    url: str | None = None
    page_id: str | None = None
    confidence: float = 0.5
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "url": self.url,
            "page_id": self.page_id,
            "confidence": round(float(self.confidence), 4),
            "note": self.note,
        }


@dataclass
class ProductProfile:
    """Agent-drafted, user-editable product profile (A2 §5.1).

    ``attributes`` is a free bag for vertical-specific / raw fields the fixed
    slots don't cover (business type, scale, contact, positioning, ...).
    """

    name: str = ""
    pitch: str = ""
    value_props: list[str] = field(default_factory=list)
    offerings: list[str] = field(default_factory=list)
    geo: list[str] = field(default_factory=list)
    price_band: str = ""
    brand: str = ""
    tone: str = ""
    assets: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "pitch": self.pitch,
            "value_props": list(self.value_props),
            "offerings": list(self.offerings),
            "geo": list(self.geo),
            "price_band": self.price_band,
            "brand": self.brand,
            "tone": self.tone,
            "assets": list(self.assets),
            "attributes": dict(self.attributes),
        }

    @classmethod
    def from_dict(cls, d: Any) -> "ProductProfile":
        if not isinstance(d, dict):
            return cls()
        return cls(
            name=str(d.get("name") or ""),
            pitch=str(d.get("pitch") or ""),
            value_props=list(d.get("value_props") or []),
            offerings=list(d.get("offerings") or []),
            geo=list(d.get("geo") or []),
            price_band=str(d.get("price_band") or ""),
            brand=str(d.get("brand") or ""),
            tone=str(d.get("tone") or ""),
            assets=list(d.get("assets") or []),
            attributes=dict(d.get("attributes") or {}),
        )

    def merge_edits(self, edits: Any) -> "ProductProfile":
        """Return a copy with user ``edits`` applied over the drafted fields.

        Shallow per-slot overwrite (present keys replace); ``attributes`` is
        merged key-wise so a partial edit doesn't drop the drafted bag."""
        if not isinstance(edits, dict) or not edits:
            return ProductProfile.from_dict(self.to_dict())
        merged = self.to_dict()
        for key, value in edits.items():
            if key == "attributes" and isinstance(value, dict):
                bag = dict(merged.get("attributes") or {})
                bag.update(value)
                merged["attributes"] = bag
            elif key in merged:
                merged[key] = value
        return ProductProfile.from_dict(merged)


@dataclass
class ProductStudyResult:
    """The A2 artifact that gates building (A2 §5.1)."""

    profile: ProductProfile
    vertical: VerticalGuess
    competitors: list[Competitor] = field(default_factory=list)
    asset_gaps: AssetGaps = field(default_factory=AssetGaps)
    # True when vertical confidence was low → mapped to ``generic`` + a tagged
    # user confirm (A2 §5.3). Drives the analyze_product elicitation.
    needs_vertical_confirm: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile.to_dict(),
            "vertical": self.vertical.to_dict(),
            "competitors": [c.to_dict() for c in self.competitors],
            "asset_gaps": self.asset_gaps.to_dict(),
            "needs_vertical_confirm": self.needs_vertical_confirm,
        }

    def summary_line(self) -> str:
        """One-line human summary for the tool receipt."""
        name = self.profile.name or "(unnamed product)"
        parts = [f"Studied {name}"]
        parts.append(f"vertical={self.vertical.code} ({self.vertical.confidence:.0%})")
        parts.append(f"{len(self.competitors)} competitor(s)")
        if self.asset_gaps.any_open():
            gaps = []
            if self.asset_gaps.logo_missing:
                gaps.append("logo")
            gaps.extend(self.asset_gaps.missing_categories)
            parts.append("missing assets: " + ", ".join(gaps))
        return " · ".join(parts)
