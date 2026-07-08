"""A3 planner — the draft role.

``draft(plan_ctx)`` proposes campaign INTENT as an RFC-7386 merge patch over the
CampaignPlan body: objective mapping, audience descriptions, keyword sets, and the
campaign/ad-group structure. It reasons only over the grounding in ``PlanContext``
(A2 profile + vertical + J3/J4 fetched options + effective J5 config). It emits
IR, never Meta/Google payloads (that is J7), and never invents platform ids
(grounding is enforced downstream in the loop, but the prompt forbids it too).

MiniMax M3. Single-shot reasoning call (no tool loop). The critic, not the
planner, is the quality bar — the planner drafts, the loop validates + critiques.
"""

from __future__ import annotations

import json
import logging

from app.agents.adzump2.planner._llm import complete_json
from app.agents.adzump2.planner.models import PlanContext, PlanPatch

logger = logging.getLogger(__name__)

DRAFT_MAX_TOKENS = 6000

_SYSTEM_PROMPT = """You are the campaign PLANNER for Adzump. Given a studied product and a target outcome, you draft a complete, coherent ad-campaign INTENT as a JSON merge patch over a platform-neutral CampaignPlan.

# What you emit
A single JSON object: {"patch": <RFC-7386 merge patch>, "rationale": "<one short paragraph>"}.
The patch carries INTENT under `body` and the platform/type selectors at top level:
- body.objective: { platformObjective (LEADS|CONVERSIONS|TRAFFIC|AWARENESS|ENGAGEMENT|SALES|APP), targetMilestone, conversionEvent? }
- body.budget: { currency, dailyBudget XOR totalBudget as {amount, currency}, split[]? }
- body.schedule: { startAt, endAt?, timezone, optimizationCadence }
- body.adGroups[]: { id, name, platform, targeting{ geo, demographics?, audiences{interests[],...}, languages[], keywords[], negativeKeywords[] }, bid?, ads[] }
- body.creatives[]: { id, format, copy?, attributes{} }
- top-level: platforms[], campaignTypes{PLATFORM: TYPE}

# Hard rules
- Emit INTENT only. NEVER emit Meta/Google API payloads, field ids, or launch state — compilation is a separate deterministic step.
- NEVER invent platform ids. Interests, keywords and audiences you name MUST be drawn from the FETCHED OPTIONS given below; audience/account/page/pixel ids MUST come from the fetched-id set. If an option was not fetched, do not use it.
- Ground objective mapping and defaults in the EFFECTIVE CONFIG (vertical defaults + overrides). Reason within them; do not reinvent them.
- Honour compliance: for a HOUSING special-ad-category vertical (real estate), do not target by age/gender/detailed demographics.
- Keep it minimal and real: only fields you can justify from the grounding. Omit what you cannot ground.
- Output the JSON object and nothing else. No prose outside the JSON.

# Section mode
If a SECTION is named, patch ONLY that section of the body (objective | targeting | structure | creatives) and leave everything else untouched (absent keys are untouched under merge-patch)."""


def _fmt(obj: object, limit: int = 4000) -> str:
    """Compact JSON for the prompt, truncated so one huge option list can't
    blow the context budget (M3 economy)."""
    try:
        text = json.dumps(obj, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(obj)
    if len(text) > limit:
        return text[:limit] + " …(truncated)"
    return text


def build_draft_prompt(ctx: PlanContext) -> str:
    """The user message for a draft call — the grounding, laid out for M3."""
    section = ctx.section or "WHOLE PLAN (all sections)"
    lines = [
        f"# Target section\n{section}",
        f"\n# Vertical (selects the playbook)\n{ctx.vertical}",
        f"\n# Target outcome (user's words)\n{ctx.goal or '(not stated — infer from profile + objective config)'}",
        f"\n# Product profile (A2)\n{_fmt(ctx.profile)}",
        f"\n# Effective config (J5 vertical defaults + overrides)\n{_fmt(ctx.effective_config)}",
        f"\n# Fetched options (the ONLY audiences/keywords you may use)\n{_fmt(ctx.fetched_options)}",
        f"\n# Fetched ids (the ONLY platform ids you may use)\n{_fmt(sorted(ctx.fetched_ids))}",
        f"\n# Current plan (patch on top of this; do not restate unchanged fields)\n{_fmt(ctx.plan)}",
        "\nDraft the merge patch now. Return ONLY the JSON object "
        '{"patch": {...}, "rationale": "..."}.',
    ]
    return "\n".join(lines)


class Planner:
    """The draft role. One M3 call per ``draft``; stateless + reusable."""

    async def draft(self, ctx: PlanContext) -> PlanPatch:
        prompt = build_draft_prompt(ctx)
        data, _usage = await complete_json(
            _SYSTEM_PROMPT, prompt, max_tokens=DRAFT_MAX_TOKENS, log_tag="adzump2.planner.draft"
        )
        patch = PlanPatch.from_llm(data)
        if patch.is_empty():
            logger.warning("planner.draft produced an empty patch (section=%s)", ctx.section)
        return patch


_planner: Planner | None = None


def get_planner() -> Planner:
    """Shared Planner singleton."""
    global _planner
    if _planner is None:
        _planner = Planner()
    return _planner


async def draft(ctx: PlanContext) -> PlanPatch:
    """Module-level convenience matching the A3 design signature."""
    return await get_planner().draft(ctx)
