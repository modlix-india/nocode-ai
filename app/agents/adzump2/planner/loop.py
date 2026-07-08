"""A3 generate loop — draft → validate → critique → repair, bounded + monotonic.

This is the net-new generation engine (the legacy retried ~3x and accepted a
degraded pool; the CFA campaign sub-agent was a stub). The contract:

    J6 (validate_plan) is the CORRECTNESS FLOOR — an invalid plan is never returned.
    The critic is the QUALITY CEILING — it raises a valid plan above threshold.

The loop (design A3 §5.2):
    1. draft(ctx)               -> PlanPatch, grounded (no invented ids)
    2. apply via update_plan    (J1)
    3. validate_plan            (J6, hard gate)
    4. up to MAX_REPAIR rounds of critique-then-repair until valid AND score >= threshold
    - monotonic: a repair that regresses validity is rolled back to the last valid state
    - bounded: never more than MAX_REPAIR repair rounds (M3-thrash guard)
    - non-convergence: return the best VALID draft + the residual critique (never invalid)

Section vs whole-plan is the SAME loop with a scoped patch — ``ctx.section`` just
flows into the draft/repair prompts; the orchestration is identical.

Exposed to A1 as the ``draft_plan`` tool; it reuses the existing
``update_plan`` / ``get_plan`` / ``validate_plan`` tools (tools/plan.py) as its
only I/O path to the plan.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from app.agents.adzump2.planner.critic import Critic, get_critic
from app.agents.adzump2.planner.models import (
    GenerateResult,
    PlanContext,
    PlanCritique,
    PlanPatch,
    ValidationResult,
)
from app.agents.adzump2.planner.planner import Planner, get_planner
from app.agents.adzump2.planner.repair import Repairer, get_repairer
from app.agents.adzump2.tools.plan import get_plan, update_plan, validate_plan
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

# Bound + threshold (design A3 §9). MAX_REPAIR per CONTRACT/AGENTS; THRESHOLD is
# a per-vertical J5 knob (ctx.threshold overrides this default).
MAX_REPAIR = 2
DEFAULT_THRESHOLD = 0.8

# Server-managed columns the loop must never diff/restore (the service owns them).
_SERVER_MANAGED = frozenset(
    {"id", "revision", "clientCode", "status", "schemaVersion", "completeness"}
)

# ── grounding (the id-honesty rule, CONTRACT §0/§6) ──────────────────────────
# Keys whose values are platform ids (or id lists). A drafted/repaired patch may
# only carry ids that were fetched this session; anything else is stripped before
# it ever reaches the plan. Text options (interests/keywords) are prompt-grounded,
# not id-checked, so they are NOT in these sets.
_ID_LIST_KEYS = frozenset(
    {"customAudienceIds", "lookalikeIds", "excludedIds", "audienceIds",
     "savedAudienceIds", "interestIds", "behaviorIds"}
)
_ID_SCALAR_KEYS = frozenset(
    {"adAccountId", "pageId", "pixelId", "productId", "catalogId",
     "datasetId", "customAudienceId", "lookalikeId"}
)


def _ground_obj(obj: Any, fetched_ids: set[str]) -> Any:
    """Recursively strip platform ids not in ``fetched_ids`` from a patch value.

    List-of-ids keys keep only fetched entries; scalar-id keys are dropped
    (set to None => delete under merge-patch) when unfetched. Everything else
    passes through untouched.
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k in _ID_LIST_KEYS and isinstance(v, list):
                out[k] = [x for x in v if not isinstance(x, str) or x in fetched_ids]
            elif k in _ID_SCALAR_KEYS and isinstance(v, str) and v:
                out[k] = v if v in fetched_ids else None  # None => delete
            else:
                out[k] = _ground_obj(v, fetched_ids)
        return out
    if isinstance(obj, list):
        return [_ground_obj(x, fetched_ids) for x in obj]
    return obj


def ground_patch(patch: PlanPatch, fetched_ids: set[str] | None) -> PlanPatch:
    """Return a copy of ``patch`` with every unfetched platform id removed.

    Enforced on BOTH draft and repair output so no unfetched id ever reaches
    the plan, regardless of what the model emitted (A1 §5.4 / J6 referential
    layer). Empty ``fetched_ids`` => no ids allowed (safe default).
    """
    ids = fetched_ids or set()
    grounded = _ground_obj(copy.deepcopy(patch.patch), ids)
    return PlanPatch(patch=grounded, rationale=patch.rationale)


def collect_ids(obj: Any) -> set[str]:
    """All platform-id-shaped values anywhere under ``obj`` (for grounding audits)."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in _ID_LIST_KEYS and isinstance(v, list):
                    found.update(x for x in v if isinstance(x, str) and x)
                elif k in _ID_SCALAR_KEYS and isinstance(v, str) and v:
                    found.add(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(obj)
    return found


# ── merge-patch diff (for monotonic rollback) ────────────────────────────────


_MISSING = object()


def _merge_patch_diff(current: Any, target: Any) -> Any:
    """RFC-7386 merge patch that turns ``current`` into ``target``.

    Used to roll a regressed plan back to the last valid snapshot: present keys
    overwrite, keys removed in ``target`` become null (delete), dicts recurse,
    lists/scalars replace wholesale.
    """
    if not isinstance(target, dict) or not isinstance(current, dict):
        return target
    patch: dict[str, Any] = {}
    for k, tv in target.items():
        cv = current.get(k, _MISSING)
        if cv is _MISSING:
            patch[k] = tv
        elif isinstance(tv, dict) and isinstance(cv, dict):
            sub = _merge_patch_diff(cv, tv)
            if sub:  # only include changed sub-trees
                patch[k] = sub
        elif cv != tv:
            patch[k] = tv
    for k in current:
        if k not in target:
            patch[k] = None  # delete
    return patch


def _restore_patch(current_plan: dict, target_plan: dict) -> dict:
    """Merge patch that restores the plan (minus server-managed columns) to
    ``target_plan``. Empty when they already match."""
    cur = {k: v for k, v in current_plan.items() if k not in _SERVER_MANAGED}
    tgt = {k: v for k, v in target_plan.items() if k not in _SERVER_MANAGED}
    return _merge_patch_diff(cur, tgt)


# ── plan I/O (reuses the J1/J6 tools; the loop's only path to the plan) ──────


class PlanIO:
    """Thin async adapter over update_plan / get_plan / validate_plan.

    The generate loop touches the plan ONLY through this, so the same tool
    layer (and offline SaasClient patch) that the chat agent uses is exercised.
    Construct with the tool ``context`` (headers + session_context.plan_id).
    """

    def __init__(self, context: dict[str, Any]) -> None:
        self._context = context

    async def apply(self, patch: dict[str, Any]) -> bool:
        if not patch:
            return True  # empty patch is a no-op success
        res = await update_plan.execute({"patch": patch}, self._context)
        if not res.success:
            logger.warning("PlanIO.apply failed: %s", res.error)
        return res.success

    async def get(self) -> dict[str, Any] | None:
        res = await get_plan.execute({}, self._context)
        return res.data if res.success and isinstance(res.data, dict) else None

    async def validate(self) -> ValidationResult:
        res = await validate_plan.execute({}, self._context)
        if not res.success:
            return ValidationResult(valid=False, errors=[res.error or "validate failed"])
        return ValidationResult.from_data(res.data)


# ── the generator ────────────────────────────────────────────────────────────


class PlanGenerator:
    """Runs the bounded, monotonic draft/validate/critique/repair loop.

    Roles are injectable (constructor args) so offline tests can script
    draft/critique/repair without an LLM; defaults are the real M3 singletons.
    """

    def __init__(
        self,
        planner: Planner | Any = None,
        critic: Critic | Any = None,
        repairer: Repairer | Any = None,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        max_repair: int = MAX_REPAIR,
    ) -> None:
        self.planner = planner or get_planner()
        self.critic = critic or get_critic()
        self.repairer = repairer or get_repairer()
        self.threshold = threshold
        self.max_repair = max_repair

    async def generate(self, ctx: PlanContext, io: PlanIO) -> GenerateResult:
        threshold = ctx.threshold if ctx.threshold is not None else self.threshold
        fetched_ids = ctx.fetched_ids or set()

        # 1. draft -> ground -> apply -> validate (the floor).
        draft_patch = ground_patch(await self.planner.draft(ctx), fetched_ids)
        await io.apply(draft_patch.patch)
        plan = await io.get() or {}
        vr = await io.validate()

        # Snapshot the last valid state for monotonic rollback.
        last_valid: dict[str, Any] | None = copy.deepcopy(plan) if vr.valid else None
        # Critic is the ceiling: only score once we are on/above the floor.
        critique: PlanCritique | None = (
            await self.critic.critique(plan) if vr.valid else None
        )

        rounds = 0
        while rounds < self.max_repair:
            if self._converged(vr, critique, threshold):
                break

            repair_patch = ground_patch(
                await self.repairer.repair(plan, critique, vr.errors), fetched_ids
            )
            if repair_patch.is_empty():
                # No fix on offer — stop rather than burn the rest of the bound.
                logger.info("repair round %d returned no patch; stopping", rounds + 1)
                break

            await io.apply(repair_patch.patch)
            rounds += 1
            new_plan = await io.get() or {}
            new_vr = await io.validate()

            if new_vr.valid:
                # Progress accepted; re-critique the improved plan.
                plan, vr = new_plan, new_vr
                last_valid = copy.deepcopy(new_plan)
                critique = await self.critic.critique(plan)
            elif last_valid is not None:
                # Regression from a valid state — roll back (monotonicity).
                logger.info(
                    "repair round %d regressed validity; rolling back to last valid",
                    rounds,
                )
                await io.apply(_restore_patch(new_plan, last_valid))
                plan = copy.deepcopy(last_valid)
                vr = await io.validate()
                # critique still describes `plan` (== last_valid), leave it.
            else:
                # Still climbing to the floor (draft was invalid, no valid yet).
                plan, vr = new_plan, new_vr

        # Finalize: never return invalid if a valid state was ever reached.
        if last_valid is not None and not vr.valid:
            await io.apply(_restore_patch(plan, last_valid))
            plan = copy.deepcopy(last_valid)
            vr = await io.validate()

        converged = self._converged(vr, critique, threshold)
        score = critique.score if critique else 0.0
        result = GenerateResult(
            plan=plan,
            valid=vr.valid,
            converged=converged,
            score=score,
            rounds=rounds,
            critique=critique,
            violations=list(vr.errors),
        )
        logger.info("generate done: %s", result.to_summary())
        return result

    @staticmethod
    def _converged(
        vr: ValidationResult, critique: PlanCritique | None, threshold: float
    ) -> bool:
        return bool(vr.valid and critique is not None and critique.score >= threshold)


_generator: PlanGenerator | None = None


def get_plan_generator() -> PlanGenerator:
    """Shared PlanGenerator singleton (real M3 roles)."""
    global _generator
    if _generator is None:
        _generator = PlanGenerator()
    return _generator


async def generate_plan(ctx: PlanContext, io: PlanIO) -> GenerateResult:
    """Module-level convenience matching the A3 design signature."""
    return await get_plan_generator().generate(ctx, io)


# ── draft_plan tool (exposed to A1) ──────────────────────────────────────────


_VALID_SECTIONS = ("objective", "targeting", "structure", "creatives")


def _build_plan_context(
    params: dict[str, Any], plan: dict[str, Any], session_ctx: dict[str, Any]
) -> PlanContext:
    """Assemble the grounding for a draft from the current plan + session state.

    A2 (profile/vertical), J3/J4 (fetched options + ids) and J5 (effective
    config) stash their outputs into the session context; this reads them
    defensively so the tool degrades to a best-effort draft if a stage has not
    run yet, rather than failing.
    """
    profile = session_ctx.get("product_profile") or session_ctx.get("profile") or {}
    vertical = (
        plan.get("vertical")
        or session_ctx.get("vertical")
        or (profile.get("vertical") if isinstance(profile, dict) else None)
        or "generic"
    )
    fetched_options = session_ctx.get("fetched_options") or {}
    fetched_ids_raw = session_ctx.get("fetched_ids") or []
    fetched_ids = {str(x) for x in fetched_ids_raw if x}
    section = params.get("section")
    if section is not None:
        section = str(section).strip().lower() or None
    threshold = params.get("threshold")
    try:
        threshold = float(threshold) if threshold is not None else None
    except (TypeError, ValueError):
        threshold = None
    return PlanContext(
        plan_id=session_ctx.get("plan_id"),
        plan=plan,
        profile=profile if isinstance(profile, dict) else {},
        vertical=str(vertical),
        goal=str(params.get("goal") or "").strip(),
        fetched_options=fetched_options if isinstance(fetched_options, dict) else {},
        fetched_ids=fetched_ids,
        effective_config=session_ctx.get("effective_config") or {},
        section=section,
        threshold=threshold,
    )


async def _draft_plan(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Draft (or repair a section of) the active plan via the A3 loop."""
    session_ctx = context.setdefault("session_context", {})
    plan_id = session_ctx.get("plan_id")
    if not plan_id:
        return ToolResult(
            success=False,
            error="No active plan in this session. Call create_plan first.",
        )

    mode = str(params.get("mode") or "whole").strip().lower()
    section = params.get("section")
    if mode == "section" and (not section or str(section).strip().lower() not in _VALID_SECTIONS):
        return ToolResult(
            success=False,
            error=f"section mode needs a section in {_VALID_SECTIONS}.",
        )
    if mode != "section":
        params = {**params, "section": None}

    io = PlanIO(context)
    plan = await io.get()
    if plan is None:
        return ToolResult(success=False, error=f"Could not read plan {plan_id}.")

    ctx = _build_plan_context(params, plan, session_ctx)
    result = await get_plan_generator().generate(ctx, io)

    # Surface the residual critique for A1's rail / the user.
    if result.critique is not None:
        session_ctx["plan_critique"] = result.critique.to_dict()

    data = {
        "valid": result.valid,
        "converged": result.converged,
        "score": result.score,
        "rounds": result.rounds,
        "violations": result.violations,
        "issues": [i.to_dict() for i in result.critique.issues] if result.critique else [],
        "rationale": ctx.section or "whole-plan",
    }
    if not result.valid:
        # Never present an invalid plan as done — tell A1 to intervene.
        return ToolResult(
            success=False,
            data=data,
            error=(
                "Could not reach a valid plan within the repair bound. "
                f"Open violations: {'; '.join(result.violations) or '(none reported)'}."
            ),
        )
    return ToolResult(success=True, data=data, summary=result.to_summary())


draft_plan = ToolDefinition(
    name="draft_plan",
    display_name="Draft Plan",
    description=(
        "Generate a complete, valid, high-quality campaign plan (or repair one "
        "section of it) with the planner/critic/repair loop. Drafts campaign "
        "INTENT grounded on the studied product profile and the fetched targeting/"
        "keyword options, validates it (the hard gate), then critiques and repairs "
        "up to twice until it is valid and clears the quality bar. Use whole-plan "
        "mode for a fast build once the product is studied; use section mode to "
        "(re)draft just objective, targeting, structure, or creatives. It edits "
        "the active plan via update_plan and never invents platform ids."
    ),
    parameters=[
        ToolParameter(
            name="mode",
            type="string",
            description="'whole' drafts the entire plan; 'section' drafts one section only.",
            required=False,
            enum=["whole", "section"],
            default="whole",
        ),
        ToolParameter(
            name="section",
            type="string",
            description="Which section to (re)draft when mode='section'.",
            required=False,
            enum=list(_VALID_SECTIONS),
        ),
        ToolParameter(
            name="goal",
            type="string",
            description=(
                "The campaign outcome in the user's words (e.g. 'site visits', "
                "'qualified leads'), used to steer objective + milestone mapping."
            ),
            required=False,
        ),
        ToolParameter(
            name="threshold",
            type="number",
            description=(
                "Optional quality bar 0..1 the critic must clear (defaults to the "
                "vertical default). Rarely set by hand."
            ),
            required=False,
        ),
    ],
    execute=_draft_plan,
)
