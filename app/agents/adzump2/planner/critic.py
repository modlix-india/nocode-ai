"""A3 critic — the quality ceiling.

``critique(plan)`` scores a drafted plan against the vertical critic rubric (J5)
and returns a ``PlanCritique`` (blended score + per-axis breakdown + actionable
issues). The critic is held to a HIGHER rigor bar than the planner: it is the
reviewer, so its prompt is adversarial and specific, and it must justify the
score on each axis rather than wave a plan through.

The critic is the ceiling, not the floor: J6 (`validate_plan`) is the hard
correctness gate; the critic raises quality *above* valid. Both roles run on
MiniMax M3.
"""

from __future__ import annotations

import json
import logging

from app.agents.adzump2.planner._llm import complete_json
from app.agents.adzump2.planner.models import PlanCritique

logger = logging.getLogger(__name__)

CRITIC_MAX_TOKENS = 3500

# The default rubric axes (a vertical playbook may extend/override via J5).
RUBRIC_AXES = [
    "objective_fit",        # objective + milestone match the stated outcome
    "targeting_coherence",  # geo/audience/keywords hang together and fit the product
    "structure_fit",        # ad-group/campaign-type split is sensible for the platforms
    "creative_angle_diversity",  # angles cover distinct value props, not one note
    "budget_sanity",        # budget/split/bid are realistic for the goal
    "compliance",           # special-ad-category + disclaimers respected
]

_SYSTEM_PROMPT = """You are the campaign CRITIC for Adzump — the reviewer, not the author. You score a drafted CampaignPlan against the vertical rubric and you are deliberately hard to satisfy. A plan that merely validates is NOT automatically good; your job is to find what a seasoned performance marketer would push back on.

# What you emit
A single JSON object and nothing else:
{
  "score": <float 0..1>,                 // blended quality; withhold high scores unless earned
  "by_axis": { "<axis>": <float 0..1>, ... },
  "issues": [ { "path": "<dotted plan path>", "severity": "error|warning|info", "suggestion": "<concrete fix>", "axis": "<axis>" }, ... ],
  "summary": "<one or two sentences>"
}

# Rubric axes (score each, then blend)
- objective_fit: does platformObjective + targetMilestone actually serve the stated outcome?
- targeting_coherence: do geo, audiences, keywords, negatives cohere and fit THIS product/vertical? Flag scattershot or off-product targeting.
- structure_fit: is the ad-group split and per-platform campaign type sensible? Flag one-giant-adgroup or mismatched type.
- creative_angle_diversity: do the creative angles cover distinct, on-strategy value props? Flag single-angle or generic copy.
- budget_sanity: are budget, split and bid realistic for the objective and geo? Flag under/over-funding.
- compliance: for HOUSING (real estate) is age/gender/detailed targeting avoided and the RERA-style disclaimer present?

# Rigor rules
- Be specific: every issue names a real dotted path and a concrete fix. No vague "improve targeting".
- Reserve severity "error" for things that make the plan weak or non-launchable in spirit (not for J6 structural gaps — those are validated separately). Use "warning" for quality gaps, "info" for polish.
- Do not inflate. If an axis is thin, score it low and say why. A plan with no real weaknesses may score high; most drafts should not.
- Output ONLY the JSON object."""


def _fmt(obj: object, limit: int = 6000) -> str:
    try:
        text = json.dumps(obj, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(obj)
    if len(text) > limit:
        return text[:limit] + " …(truncated)"
    return text


def build_critique_prompt(plan: dict) -> str:
    vertical = plan.get("vertical") or "generic"
    return (
        f"# Vertical\n{vertical}\n\n"
        f"# Rubric axes to score\n{', '.join(RUBRIC_AXES)}\n\n"
        f"# Plan under review\n{_fmt(plan)}\n\n"
        "Score every axis, list concrete issues, and blend into an overall score. "
        "Return ONLY the JSON object."
    )


class Critic:
    """The scoring role. One M3 call per ``critique``; stateless + reusable."""

    async def critique(self, plan: dict) -> PlanCritique:
        prompt = build_critique_prompt(plan or {})
        data, _usage = await complete_json(
            _SYSTEM_PROMPT, prompt, max_tokens=CRITIC_MAX_TOKENS, log_tag="adzump2.planner.critique"
        )
        crit = PlanCritique.from_llm(data)
        logger.info("critic scored plan: %.3f (%d issues)", crit.score, len(crit.issues))
        return crit


_critic: Critic | None = None


def get_critic() -> Critic:
    """Shared Critic singleton."""
    global _critic
    if _critic is None:
        _critic = Critic()
    return _critic


async def critique(plan: dict) -> PlanCritique:
    """Module-level convenience matching the A3 design signature."""
    return await get_critic().critique(plan)
