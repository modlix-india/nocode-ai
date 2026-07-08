"""A5 diagnose output contract — the ``Diagnosis`` the agent emits.

Mirrors A5-diagnose.md §5.1:

    Diagnosis {
      narrative:      str,                 # plain-language "what's happening and why"
      ranked_actions: list[AnnotatedAction],# J12 actions + priority + business-framed why
      test_proposals: list[TestProposal],  # qualitative angles/audiences to try (→ A4/J21)
      watchlist:      list[WatchItem]       # thin/immature grains to WATCH, not act on
    }

A5 produces NO numbers and applies NOTHING. Every number on an ``AnnotatedAction``
is carried **verbatim** from the J12 ``ActionSet`` (A5 narrates + prioritizes, it
never recomputes). ``TestProposal``s are grounded in real gaps in the J20
attribute map; ``WatchItem``s are grains J12 declined on immature/FAST_ONLY signal.

Pydantic models (per the slice contract); ``to_dict`` gives the tool's wire shape.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AnnotatedAction(BaseModel):
    """A single J12 ``Action``, narrated + prioritized by A5.

    A5 attaches ``priority`` (1 = act first) and a business-framed ``why``; it
    copies J12's ``change`` + numbers + gate verdicts **verbatim**. A5 never
    invents an act-now action (those go through ``propose_action`` → the J12
    gates), so every AnnotatedAction corresponds to a real gated J12 action.
    """

    model_config = ConfigDict(extra="ignore")

    type: str
    target_id: str = ""
    change: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""          # J12's quantitative rationale (verbatim)
    why: str = ""                # A5's business-framed 'why this matters first'
    priority: int = 0            # 1 = act first; 0 = unranked

    # ── J12 numbers + gate verdicts — carried VERBATIM (A5 recomputes none) ──
    expected_delta: float | None = None
    confidence: float | None = None
    significance_verdict: str = ""
    risk: str = ""
    requires_approval: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "targetId": self.target_id,
            "change": dict(self.change),
            "rationale": self.rationale,
            "why": self.why,
            "priority": self.priority,
            "expectedDelta": self.expected_delta,
            "confidence": self.confidence,
            "significanceVerdict": self.significance_verdict,
            "risk": self.risk,
            "requiresApproval": self.requires_approval,
        }


class TestProposal(BaseModel):
    """A qualitative creative-angle / audience hypothesis to try next.

    Routed to A4 (generate) / J21 (controlled experiment). The engine keeps only
    proposals ``grounded`` in a real gap in the J20 attribute map (an
    under-explored or junk-concentrated axis+value), so A5 proposes tests the
    quantitative engine can't invent — never a random guess.
    """

    model_config = ConfigDict(extra="ignore")

    hypothesis: str
    angle: str = ""
    audience: str = ""
    rationale: str = ""
    route: str = "A4"                                  # A4 | J21
    grounds_on: dict[str, str] = Field(default_factory=dict)  # {"axis":..,"value":..}
    grounded: bool = False                             # code-verified vs the J20 gap set

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis": self.hypothesis,
            "angle": self.angle,
            "audience": self.audience,
            "rationale": self.rationale,
            "route": self.route,
            "groundsOn": dict(self.grounds_on),
            "grounded": self.grounded,
        }


class WatchItem(BaseModel):
    """A grain with thin / immature (FAST_ONLY) signal — keep WATCHING, do NOT
    act on it yet. A5 never pushes an immature grain into ``ranked_actions``;
    fast signal proposes, slow signal disposes (A5-diagnose.md §5.3)."""

    model_config = ConfigDict(extra="ignore")

    target_id: str
    grain: str = ""
    signal_maturity: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "targetId": self.target_id,
            "grain": self.grain,
            "signalMaturity": self.signal_maturity,
            "reason": self.reason,
        }


class Diagnosis(BaseModel):
    """A5's full output: a plain-language narrative + prioritized J12 actions +
    grounded test proposals + a watchlist. Plus deterministic ``signals`` (the
    winning attributes / junk sources / thin grains / attribute gaps the code
    located) and an audit trail (``llm_calls``, ``warnings``)."""

    model_config = ConfigDict(extra="ignore")

    narrative: str = ""
    ranked_actions: list[AnnotatedAction] = Field(default_factory=list)
    test_proposals: list[TestProposal] = Field(default_factory=list)
    watchlist: list[WatchItem] = Field(default_factory=list)

    # ── deterministic grounding + audit (not LLM-authored) ──
    signals: dict[str, Any] = Field(default_factory=dict)
    llm_calls: int = 0
    warnings: list[str] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "narrative": self.narrative,
            "rankedActions": [a.to_dict() for a in self.ranked_actions],
            "testProposals": [t.to_dict() for t in self.test_proposals],
            "watchlist": [w.to_dict() for w in self.watchlist],
            "signals": dict(self.signals),
            "counts": {
                "rankedActions": len(self.ranked_actions),
                "testProposals": len(self.test_proposals),
                "watchlist": len(self.watchlist),
            },
            "llmCalls": self.llm_calls,
            "warnings": list(self.warnings),
        }
