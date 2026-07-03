"""A3 planner/critic/repair — the structured contracts the engine passes around.

All platform-neutral, all IR-level (CONTRACT.md §1). Nothing here is a Meta/Google
payload (that is J7) and nothing invents platform ids (J6 referential rule). The three
roles speak these shapes:

    draft(plan_ctx)                 -> PlanPatch      # IR intent as an RFC-7386 merge patch
    critique(plan)                  -> PlanCritique   # score + by-axis + issues vs the J5 rubric
    repair(plan, critique, errors)  -> PlanPatch      # a targeted, bounded fix

``PlanContext`` is the grounding the planner reasons over (A2 profile + vertical +
J3/J4 fetched options + effective J5 config). ``GenerateResult`` is what the loop
returns to A1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── grounding input ──────────────────────────────────────────────────────────


@dataclass
class PlanContext:
    """Everything the planner grounds a draft on — no invention beyond this.

    The planner proposes objective mapping, audiences, keywords and structure
    *from* these fields; ids it emits must come from ``fetched_ids`` (the
    id-honesty rule, CONTRACT §0/§6), audiences/keywords from ``fetched_options``.
    """

    plan_id: str | None
    plan: dict[str, Any]  # current server-side CampaignPlan snapshot (may be sparse)
    profile: dict[str, Any] = field(default_factory=dict)  # A2 ProductProfile
    vertical: str = "generic"  # A2 VerticalGuess.code -> selects the J5 playbook
    goal: str = ""  # the objective/outcome in the user's words (e.g. "site visits")
    fetched_options: dict[str, Any] = field(default_factory=dict)  # J3/J4 candidates
    fetched_ids: set[str] = field(default_factory=set)  # real ids fetched this session
    effective_config: dict[str, Any] = field(default_factory=dict)  # J5 defaults + overrides
    section: str | None = None  # None => whole-plan; else "objective"|"targeting"|"structure"|"creatives"
    threshold: float | None = None  # per-vertical quality bar (None => generator default)


# ── planner / repair output ──────────────────────────────────────────────────


@dataclass
class PlanPatch:
    """An RFC-7386 merge patch over the CampaignPlan, plus why.

    ``patch`` is exactly what the ``update_plan`` (J1) tool takes: present keys
    overwrite, ``null`` deletes, absent keys are untouched. IR intent lives under
    ``patch["body"]`` (objective / budget / schedule / adGroups / creatives ...);
    the platform/type selectors (``platforms``, ``campaignTypes``) sit top-level.
    """

    patch: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""

    def is_empty(self) -> bool:
        return not bool(self.patch)

    @classmethod
    def from_llm(cls, data: dict[str, Any] | None) -> "PlanPatch":
        """Parse a planner/repair LLM JSON object into a PlanPatch.

        Accepts either ``{"patch": {...}, "rationale": "..."}`` or a bare merge
        patch object (treated as the patch itself). A non-dict / empty parse
        yields an empty patch, which the loop treats as a no-op.
        """
        if not isinstance(data, dict):
            return cls()
        inner = data.get("patch")
        if isinstance(inner, dict):
            return cls(patch=inner, rationale=str(data.get("rationale") or ""))
        # Bare merge-patch fallback: strip our own envelope keys, keep the rest.
        bare = {k: v for k, v in data.items() if k not in ("rationale", "patch")}
        return cls(patch=bare, rationale=str(data.get("rationale") or ""))


# ── critic output ─────────────────────────────────────────────────────────────


@dataclass
class Issue:
    """One critic finding, anchored to a dotted plan path."""

    path: str
    severity: str  # "error" | "warning" | "info"
    suggestion: str
    axis: str = ""  # which rubric axis it maps to (targeting / structure / creative ...)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "severity": self.severity,
            "suggestion": self.suggestion,
            "axis": self.axis,
        }

    @classmethod
    def from_llm(cls, data: Any) -> "Issue | None":
        if not isinstance(data, dict):
            return None
        path = str(data.get("path") or "").strip()
        suggestion = str(data.get("suggestion") or "").strip()
        if not path and not suggestion:
            return None
        sev = str(data.get("severity") or "warning").strip().lower()
        if sev not in ("error", "warning", "info"):
            sev = "warning"
        return cls(
            path=path or "(plan)",
            severity=sev,
            suggestion=suggestion,
            axis=str(data.get("axis") or "").strip(),
        )


@dataclass
class PlanCritique:
    """Structured quality verdict for a drafted plan (the ceiling, above J6).

    ``score`` is a blended 0..1 quality score; ``by_axis`` breaks it out per J5
    rubric axis; ``issues`` are the actionable findings a repair round consumes.
    """

    score: float
    by_axis: dict[str, float] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    summary: str = ""

    @property
    def blocking_issues(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "by_axis": dict(self.by_axis),
            "issues": [i.to_dict() for i in self.issues],
            "summary": self.summary,
        }

    @classmethod
    def from_llm(cls, data: dict[str, Any] | None) -> "PlanCritique":
        """Parse a critic LLM JSON object; a failed parse yields a fail-safe
        zero-score critique (never a silent pass — quality must be earned)."""
        if not isinstance(data, dict):
            return cls(score=0.0, summary="critic returned no parseable verdict",
                       issues=[Issue("(plan)", "info", "re-run the critic", "")])
        raw_issues = data.get("issues")
        issues: list[Issue] = []
        if isinstance(raw_issues, list):
            for item in raw_issues:
                parsed = Issue.from_llm(item)
                if parsed is not None:
                    issues.append(parsed)
        by_axis: dict[str, float] = {}
        raw_axis = data.get("by_axis")
        if isinstance(raw_axis, dict):
            for k, v in raw_axis.items():
                try:
                    by_axis[str(k)] = _clamp01(float(v))
                except (TypeError, ValueError):
                    continue
        score = data.get("score")
        try:
            score_f = _clamp01(float(score))
        except (TypeError, ValueError):
            # No overall score but per-axis present -> average them.
            score_f = round(sum(by_axis.values()) / len(by_axis), 4) if by_axis else 0.0
        return cls(
            score=score_f,
            by_axis=by_axis,
            issues=issues,
            summary=str(data.get("summary") or "").strip(),
        )


# ── validation (J6) + loop result ─────────────────────────────────────────────


@dataclass
class ValidationResult:
    """Normalized J6 verdict. The correctness floor: invalid never ships."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    status: str = ""

    @classmethod
    def from_data(cls, data: Any) -> "ValidationResult":
        """Normalize a ``validate_plan`` payload. Java may key issues as
        ``issues``; the P0 fake keys them ``errors`` — accept either."""
        if not isinstance(data, dict):
            return cls(valid=False, errors=["validate returned no verdict"])
        raw = data.get("errors")
        if raw is None:
            raw = data.get("issues")
        errors: list[str] = []
        if isinstance(raw, list):
            for e in raw:
                if isinstance(e, str):
                    errors.append(e)
                elif isinstance(e, dict):
                    errors.append(str(e.get("message") or e.get("path") or e))
        return cls(
            valid=bool(data.get("valid")),
            errors=errors,
            status=str(data.get("status") or ""),
        )


@dataclass
class GenerateResult:
    """What the generate loop hands back to A1.

    ``valid`` reflects the RETURNED plan (never invalid once validity was
    reached — the loop is monotonic). ``converged`` means valid AND the critic
    cleared the threshold. On non-convergence, ``plan`` is the best valid draft
    and ``critique`` carries the residual issues for A1 to surface.
    """

    plan: dict[str, Any]
    valid: bool
    converged: bool
    score: float
    rounds: int
    critique: PlanCritique | None = None
    violations: list[str] = field(default_factory=list)

    def to_summary(self) -> str:
        parts = [
            f"valid={self.valid}",
            f"score={self.score:.2f}",
            f"repairs={self.rounds}",
            "converged" if self.converged else "needs-review",
        ]
        if self.violations:
            parts.append(f"open violations: {'; '.join(self.violations[:3])}")
        elif self.critique and not self.converged and self.critique.issues:
            top = self.critique.issues[0]
            parts.append(f"top issue: {top.path} — {top.suggestion}")
        return ", ".join(parts)


def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return round(x, 4)
