"""A4 creative output contract — dataclasses for the Creative[] + LeadForm.

Mirrors A4-creative.md §5.1 and CONTRACT.md §1.3 / §1.5:

    Creative { id, format, copy: Copy, attributes, asset_refs, predict_score }
    Copy     { headlines, primary_texts, descriptions, cta }          # slot POOLS; J7 maps to platform slots
    LeadForm { fields, privacy_url, thankyou }

``predict_score`` is STUBBED to ``None`` in P1 — the J20 Java/ML performance
predictor (never an LLM) will score creatives pre-spend later. A4 emits and does
not score performance (AGENTS boundary). See ``PREDICT_TODO``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


PREDICT_TODO = (
    "TODO(J20): predict_score is stubbed to None in P1. The J20 Java/ML "
    "performance predictor (never an LLM) scores creatives pre-spend and applies "
    "the vertical floor; A4 emits creatives and calls J20 to score. A4 never "
    "scores performance itself."
)

# Disposition after the pre-spend critic gate (predict is stubbed in P1).
LAUNCH = "LAUNCH"    # critic-passed → eligible for the launchable plan
EXPLORE = "EXPLORE"  # below the critic floor after bounded repair → J21 experiment candidate, not launch


@dataclass
class Copy:
    """Copy emitted as slot POOLS; J7 maps pools onto platform slots."""

    headlines: list[str] = field(default_factory=list)
    primary_texts: list[str] = field(default_factory=list)
    descriptions: list[str] = field(default_factory=list)
    cta: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "headlines": list(self.headlines),
            "primary_texts": list(self.primary_texts),
            "descriptions": list(self.descriptions),
            "cta": self.cta,
        }


@dataclass
class ImageBrief:
    """A brief for one visual asset. Image generation (MCP ``generate_image`` /
    compositing) is a P1 TODO stub — the brief + placeholder asset refs are
    emitted so J7 can compile once J16 stores real assets."""

    scene: str = ""
    subject: str = ""
    style: str = ""
    overlay_text: str = ""
    aspect_ratios: list[str] = field(default_factory=list)
    route: str = "GENERATE"          # GENERATE | PICK_EXISTING
    status: str = "STUBBED"          # STUBBED (P1) | GENERATED | PICKED
    todo: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene": self.scene,
            "subject": self.subject,
            "style": self.style,
            "overlayText": self.overlay_text,
            "aspectRatios": list(self.aspect_ratios),
            "route": self.route,
            "status": self.status,
            "todo": self.todo,
        }


@dataclass
class Creative:
    """One generated creative — copy pools + taxonomy attributes + asset refs."""

    id: str
    format: str                                   # RSA | IMAGE | VIDEO | CAROUSEL | DEMAND_GEN
    copy: Copy
    attributes: dict[str, Any] = field(default_factory=dict)  # keyed to the J5 taxonomy axes
    asset_refs: list[str] = field(default_factory=list)
    predict_score: float | None = None            # STUBBED None in P1 (J20 later)

    # audit / downstream (not in the minimal contract, but carried for the plan + loop)
    source: str = "GENERATED"                      # GENERATED | IMPORTED | MARKET
    image_brief: ImageBrief | None = None
    critic_score: float | None = None
    critic_issues: list[str] = field(default_factory=list)
    attribute_warnings: list[str] = field(default_factory=list)
    pool_shortfalls: list[str] = field(default_factory=list)
    disposition: str = LAUNCH
    predict_note: str = PREDICT_TODO

    def to_dict(self) -> dict[str, Any]:
        """Full A4 view (debug / return payload)."""
        return {
            "id": self.id,
            "format": self.format,
            "copy": self.copy.to_dict(),
            "attributes": dict(self.attributes),
            "asset_refs": list(self.asset_refs),
            "predict_score": self.predict_score,
            "source": self.source,
            "imageBrief": self.image_brief.to_dict() if self.image_brief else None,
            "criticScore": self.critic_score,
            "criticIssues": list(self.critic_issues),
            "attributeWarnings": list(self.attribute_warnings),
            "poolShortfalls": list(self.pool_shortfalls),
            "disposition": self.disposition,
            "predictNote": self.predict_note,
        }

    def to_plan_creative(self) -> dict[str, Any]:
        """Serialize to the CampaignPlan ``body.creatives[]`` shape (CONTRACT §1.3).

        Copy stays as POOLS (headlines[]/descriptions[]/primaryTexts[]) — J7 maps
        pools onto platform slots at compile time. ``predictScore`` is null in P1.
        """
        return {
            "id": self.id,
            "format": self.format,
            "assetRefs": list(self.asset_refs),
            "copy": {
                "headlines": list(self.copy.headlines),
                "primaryTexts": list(self.copy.primary_texts),
                "descriptions": list(self.copy.descriptions),
                "cta": self.copy.cta,
            },
            "attributes": dict(self.attributes),
            "predictScore": self.predict_score,  # None in P1
            "source": self.source,
        }


@dataclass
class LeadFormField:
    """One lead-form field. Standard fields serialize to a bare string on the
    wire (CONTRACT §1.5 ``["FULL_NAME","PHONE",...]``); CHOICE/typed fields
    serialize to an object."""

    key: str
    type: str                                     # FULL_NAME | PHONE | EMAIL | CHOICE | SHORT_TEXT | CITY
    label: str = ""
    options: list[str] = field(default_factory=list)
    required: bool = True

    _STANDARD = {"FULL_NAME", "PHONE", "EMAIL"}

    def to_wire(self) -> str | dict[str, Any]:
        if self.type in self._STANDARD and not self.options and not self.label:
            return self.type
        out: dict[str, Any] = {"key": self.key, "type": self.type, "required": self.required}
        if self.label:
            out["label"] = self.label
        if self.options:
            out["options"] = list(self.options)
        return out


@dataclass
class LeadForm:
    """Meta instant form / Google lead-form extension spec (CONTRACT §1.5)."""

    fields: list[LeadFormField] = field(default_factory=list)
    privacy_url: str = ""
    thankyou: str = ""
    source: str = "GENERATED"                     # GENERATED | FALLBACK

    def to_dict(self) -> dict[str, Any]:
        return {
            "fields": [f.to_wire() for f in self.fields],
            "privacy_url": self.privacy_url,
            "thankyou": self.thankyou,
            "source": self.source,
        }

    def to_plan_lead_form(self) -> dict[str, Any]:
        """Serialize to the CampaignPlan ``body.leadForm`` shape (CONTRACT §1.5)."""
        return {
            "fields": [f.to_wire() for f in self.fields],
            "privacyPolicyUrl": self.privacy_url,
            "thankYouMessage": self.thankyou,
        }


@dataclass
class CreativeAngle:
    """A strategy angle: the taxonomy angle + a proposed attribute bag + why."""

    angle: str
    rationale: str = ""
    strategy: str = "explore"                     # P1: explore-only (exploit is TODO J19/J20)
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class CreativeSet:
    """A4's full output: attribute-tagged creatives + a lead form, gated."""

    vertical: str
    creatives: list[Creative] = field(default_factory=list)
    lead_form: LeadForm | None = None
    angles: list[CreativeAngle] = field(default_factory=list)
    predict: dict[str, Any] = field(default_factory=dict)   # {status, todo, scored, floor}
    warnings: list[str] = field(default_factory=list)
    llm_calls: int = 0

    @property
    def launchable(self) -> list[Creative]:
        return [c for c in self.creatives if c.disposition == LAUNCH]

    @property
    def explore(self) -> list[Creative]:
        return [c for c in self.creatives if c.disposition == EXPLORE]

    def to_dict(self) -> dict[str, Any]:
        return {
            "vertical": self.vertical,
            "creatives": [c.to_dict() for c in self.creatives],
            "leadForm": self.lead_form.to_dict() if self.lead_form else None,
            "angles": [
                {"angle": a.angle, "rationale": a.rationale, "strategy": a.strategy,
                 "attributes": dict(a.attributes)}
                for a in self.angles
            ],
            "predict": dict(self.predict),
            "warnings": list(self.warnings),
            "counts": {
                "total": len(self.creatives),
                "launchable": len(self.launchable),
                "explore": len(self.explore),
            },
            "llmCalls": self.llm_calls,
        }
