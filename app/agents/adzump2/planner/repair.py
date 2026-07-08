"""A3 repair — the targeted, bounded fix role.

``repair(plan, critique, violations)`` proposes the SMALLEST merge patch that
clears the J6 structural violations first (the correctness floor) and then the
critic's top issues (the quality ceiling). It is deliberately conservative: it
touches only the paths named by the violations/issues, never rewrites the whole
plan, and stays grounded exactly like the planner (no invented ids, options from
the fetched set only).

MiniMax M3. Bounded by the loop (``MAX_REPAIR`` rounds); this role just emits one
focused patch per call.
"""

from __future__ import annotations

import json
import logging

from app.agents.adzump2.planner._llm import complete_json
from app.agents.adzump2.planner.models import PlanCritique, PlanPatch

logger = logging.getLogger(__name__)

REPAIR_MAX_TOKENS = 4000

_SYSTEM_PROMPT = """You are the campaign REPAIR role for Adzump. You are handed a CampaignPlan, the hard structural VIOLATIONS from the validator (J6), and the CRITIC's issues. You emit the SMALLEST merge patch that fixes them.

# What you emit
A single JSON object and nothing else: {"patch": <RFC-7386 merge patch>, "rationale": "<what you changed and why>"}.

# Priority
1. Clear every VALIDATION VIOLATION first — those are the correctness floor; the plan cannot ship until they are gone.
2. Then address the CRITIC issues, hardest (severity=error, then warning) first.

# Hard rules
- Minimal + targeted: patch ONLY the paths named by the violations/issues. Do not restate or rewrite unchanged parts of the plan (absent keys are untouched under merge-patch; set a key to null only to delete it).
- Stay grounded: any interest/keyword/audience must come from the FETCHED OPTIONS; any platform id from the FETCHED IDS. Never invent ids or options.
- Emit INTENT only (same IR shape as the plan body), never platform payloads.
- Do not regress: keep every field that is already correct. Fix, don't replace.
- Output ONLY the JSON object."""


def _fmt(obj: object, limit: int = 5000) -> str:
    try:
        text = json.dumps(obj, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(obj)
    if len(text) > limit:
        return text[:limit] + " …(truncated)"
    return text


def build_repair_prompt(
    plan: dict, critique: PlanCritique | None, violations: list[str]
) -> str:
    viol = violations or []
    issues = []
    if critique is not None:
        issues = [i.to_dict() for i in critique.issues]
    return (
        f"# Validation violations (clear these FIRST — correctness floor)\n{_fmt(viol)}\n\n"
        f"# Critic issues (address after violations, hardest first)\n{_fmt(issues)}\n\n"
        f"# Current plan\n{_fmt(plan)}\n\n"
        "Emit the smallest merge patch that clears the violations and the top issues. "
        'Return ONLY the JSON object {"patch": {...}, "rationale": "..."}.'
    )


class Repairer:
    """The repair role. One M3 call per ``repair``; stateless + reusable."""

    async def repair(
        self, plan: dict, critique: PlanCritique | None, violations: list[str]
    ) -> PlanPatch:
        prompt = build_repair_prompt(plan or {}, critique, violations or [])
        data, _usage = await complete_json(
            _SYSTEM_PROMPT, prompt, max_tokens=REPAIR_MAX_TOKENS, log_tag="adzump2.planner.repair"
        )
        patch = PlanPatch.from_llm(data)
        if patch.is_empty():
            logger.warning(
                "repair produced an empty patch (%d violations, %d issues)",
                len(violations or []),
                len(critique.issues) if critique else 0,
            )
        return patch


_repairer: Repairer | None = None


def get_repairer() -> Repairer:
    """Shared Repairer singleton."""
    global _repairer
    if _repairer is None:
        _repairer = Repairer()
    return _repairer


async def repair(
    plan: dict, critique: PlanCritique | None, violations: list[str]
) -> PlanPatch:
    """Module-level convenience matching the A3 design signature."""
    return await get_repairer().repair(plan, critique, violations)
