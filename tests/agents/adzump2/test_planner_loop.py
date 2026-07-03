"""Unit: app/agents/adzump2/planner — the A3 draft/validate/critique/repair loop.

DETERMINISTIC: no network, no LLM, no DB. The three roles (draft/critique/repair)
are injected as SCRIPTED fakes returning canned patches/critique, so the loop's
control flow is proven below the model. Plan I/O runs through the REAL
update_plan / get_plan / validate_plan tools, patched (via the eval harness's
PatchedSaasClient) onto an in-memory backend whose ``/validate`` mirrors the
Java STRUCTURAL rules (completeness + objective.platformObjective + budget XOR),
not just slot completeness — so the loop is exercised against real validity
transitions.

Proven properties (A3 §5.2 / §8):
  - converges to valid + threshold within the repair bound;
  - repairs an injected-invalid draft to valid BEFORE returning;
  - never returns an invalid plan (validity floor);
  - never exceeds MAX_REPAIR rounds (bounded, M3-thrash guard);
  - monotonic: a repair that regresses validity is rolled back;
  - grounding: no unfetched platform id ever reaches the plan.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump2.test_planner_loop -v
"""

from __future__ import annotations

import asyncio
import copy
import unittest
from typing import Any
from unittest import mock

from app.config import settings

# Provider-key checks must never bite an offline unit test.
for _key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "MINIMAX_API_KEY"):
    if not getattr(settings, _key, ""):
        setattr(settings, _key, "offline-test-key")

from app.core.tools.base import ToolResult
from app.core.tools.http_client import SaasClient
from app.agents.adzump2.tools.plan import create_plan
from app.agents.adzump2.planner import loop as loop_mod
from app.agents.adzump2.planner.loop import (
    DEFAULT_THRESHOLD,
    MAX_REPAIR,
    PlanGenerator,
    PlanIO,
    collect_ids,
    draft_plan,
    ground_patch,
)
from app.agents.adzump2.planner.models import (
    Issue,
    PlanContext,
    PlanCritique,
    PlanPatch,
)

from tests.agents.adzump2.fixtures import (
    AD_GROUPS,
    BUDGET,
    CREATIVES,
    OBJECTIVE,
    REQUIRED_SLOTS,
    SCHEDULE,
)

# ── reuse the P0 offline harness (backend + SaasClient patch) ────────────────
# Preferred: import it straight from scripts/adzump2/eval.py so the A3 loop is
# proven against the exact same fake service semantics the P0 slice uses. That
# module pulls in the full agent (and thus the shared tool registry); if a
# CONCURRENT P1 build has a sibling package (product/creative tools) that cannot
# import on this interpreter, the reuse import fails through no fault of the A3
# code. To keep this slice offline-provable regardless, fall back to a minimal
# self-contained copy of ONLY the harness pieces this test drives. Same
# semantics either way — the assertions do not care which path loaded them.
try:  # pragma: no cover - path taken depends on concurrent build state
    from scripts.adzump2.eval import (  # type: ignore
        FakePlanBackend,
        PatchedSaasClient,
        derive_completeness,
    )
except Exception:  # ImportError or a transitive failure in a sibling P1 slice
    import copy as _copy
    import re as _re

    _SERVER_FIELDS = {"id", "revision", "clientCode", "status", "schemaVersion"}

    def _merge_patch(target: Any, patch: Any) -> Any:
        """RFC 7386 merge patch (mirror of eval.merge_patch)."""
        if not isinstance(patch, dict):
            return _copy.deepcopy(patch)
        result: dict[str, Any] = dict(target) if isinstance(target, dict) else {}
        for key, value in patch.items():
            if value is None:
                result.pop(key, None)
            else:
                result[key] = _merge_patch(result.get(key), value)
        return result

    def derive_completeness(plan: dict[str, Any]) -> dict[str, Any]:
        """Mirror of eval.derive_completeness / the Java P0 rules."""
        body = plan.get("body") or {}
        checks = {
            "name": bool(plan.get("name")),
            "productId": bool(plan.get("productId")),
            "objective": bool(body.get("objective")),
            "budget": bool(body.get("budget")),
            "schedule": bool(body.get("schedule")),
            "adGroups": bool(body.get("adGroups")) or bool(body.get("assetGroups")),
            "creatives": bool(body.get("creatives")),
        }
        missing = [s for s in REQUIRED_SLOTS if not checks[s]]
        return {
            "complete": not missing,
            "missingRequired": missing,
            "filled": [s for s in REQUIRED_SLOTS if checks[s]],
            "requiredSlots": list(REQUIRED_SLOTS),
        }

    class FakePlanBackend:  # type: ignore[no-redef]
        """Minimal in-memory stand-in for the Java plan service (eval mirror)."""

        def __init__(self) -> None:
            self.plans: dict[str, dict[str, Any]] = {}
            self._seq = 0

        def create(self, seed, headers) -> ToolResult:
            self._seq += 1
            plan: dict[str, Any] = {
                "schemaVersion": "1.0", "id": f"cp_EVAL{self._seq:04d}",
                "revision": 1, "clientCode": headers.get("clientCode", "SYSTEM"),
                "status": "DRAFT", "name": None, "productId": None, "body": {},
            }
            if seed:
                plan = _merge_patch(plan, {k: v for k, v in seed.items()
                                           if k not in _SERVER_FIELDS})
            self.plans[plan["id"]] = plan
            return ToolResult(success=True, data=self._with_completeness(plan),
                              summary=f"POST /api/adzump/plans -> 200 ({plan['id']})")

        def get(self, plan_id) -> ToolResult:
            plan = self.plans.get(plan_id)
            return (self._not_found(plan_id) if plan is None
                    else ToolResult(success=True, data=self._with_completeness(plan)))

        def patch(self, plan_id, patch) -> ToolResult:
            plan = self.plans.get(plan_id)
            if plan is None:
                return self._not_found(plan_id)
            if not isinstance(patch, dict) or not patch:
                return ToolResult(success=False, error="HTTP 400: merge patch body required")
            merged = _merge_patch(plan, {k: v for k, v in patch.items()
                                         if k not in _SERVER_FIELDS})
            merged["revision"] = plan["revision"] + 1
            self.plans[plan_id] = merged
            return ToolResult(success=True, data=self._with_completeness(merged),
                              summary=f"PATCH /api/adzump/plans/{plan_id} -> 200")

        def completeness(self, plan_id) -> ToolResult:
            plan = self.plans.get(plan_id)
            return (self._not_found(plan_id) if plan is None
                    else ToolResult(success=True, data=derive_completeness(plan)))

        def validate(self, plan_id) -> ToolResult:
            plan = self.plans.get(plan_id)
            if plan is None:
                return self._not_found(plan_id)
            comp = derive_completeness(plan)
            if comp["complete"]:
                plan["status"] = "VALIDATED"
                return ToolResult(success=True, data={"valid": True, "errors": [], "status": "VALIDATED"})
            return ToolResult(success=True, data={
                "valid": False,
                "errors": [f"missing required slot: {s}" for s in comp["missingRequired"]],
                "status": plan["status"]})

        def handle(self, method, path, body, headers) -> ToolResult:
            path = (path if path.startswith("/") else f"/{path}").rstrip("/")
            m = _re.fullmatch(
                r"/api/adzump/plans(?:/([^/]+))?(?:/(completeness|validate))?", path)
            if not m:
                return ToolResult(success=False, error=f"HTTP 404: no route {method} {path}")
            plan_id, sub = m.group(1), m.group(2)
            if method == "POST" and plan_id is None:
                return self.create(body if isinstance(body, dict) else None, headers)
            if plan_id is None:
                return ToolResult(success=False, error=f"HTTP 405: {method} {path}")
            if sub == "completeness" and method == "GET":
                return self.completeness(plan_id)
            if sub == "validate" and method == "POST":
                return self.validate(plan_id)
            if sub is None and method == "GET":
                return self.get(plan_id)
            if sub is None and method == "PATCH":
                return self.patch(plan_id, body)
            return ToolResult(success=False, error=f"HTTP 405: {method} {path}")

        @staticmethod
        def _not_found(plan_id) -> ToolResult:
            return ToolResult(success=False, error=f"HTTP 404: Plan not found: {plan_id}")

        @staticmethod
        def _with_completeness(plan) -> dict[str, Any]:
            out = _copy.deepcopy(plan)
            out["completeness"] = derive_completeness(plan)
            return out

    class PatchedSaasClient:  # type: ignore[no-redef]
        """Route SaasClient traffic to the FakePlanBackend (eval mirror)."""

        def __init__(self, backend: "FakePlanBackend") -> None:
            self.backend = backend
            self._original = None

        def __enter__(self) -> "PatchedSaasClient":
            backend = self.backend

            async def _request(_self, method, path, headers=None, json=None, params=None):
                url = path if path.startswith("/") else f"/{path}"
                if url.startswith("/api/adzump/"):
                    return backend.handle(method, url, json, headers or {})
                return ToolResult(success=False,
                                  error=f"offline-eval: blocked non-adzump call {method} {url}")

            self._original = SaasClient._request
            SaasClient._request = _request
            return self

        def __exit__(self, *exc: Any) -> None:
            SaasClient._request = self._original

AUTH_HEADERS = {
    "Authorization": "Bearer offline-test",
    "clientCode": "SYSTEM",
    "appCode": "adzump",
}
SEED = {"name": "Whitefield Launch - Site Visits", "productId": "prd_5521"}


# ── structural /validate backend (mirrors the Java J6 structural rules) ──────


def structural_errors(plan: dict[str, Any]) -> list[str]:
    """Completeness PLUS a couple of nested structural rules (CONTRACT §6):
    objective needs platformObjective; budget is dailyBudget XOR totalBudget."""
    errors = [
        f"missing required slot: {s}"
        for s in derive_completeness(plan)["missingRequired"]
    ]
    body = plan.get("body") or {}
    obj = body.get("objective")
    if isinstance(obj, dict) and not obj.get("platformObjective"):
        errors.append("objective.platformObjective is required")
    budget = body.get("budget")
    if isinstance(budget, dict):
        has_daily = budget.get("dailyBudget") is not None
        has_total = budget.get("totalBudget") is not None
        if has_daily == has_total:
            errors.append("budget must set exactly one of dailyBudget / totalBudget")
    return errors


class StructuralPlanBackend(FakePlanBackend):
    """FakePlanBackend with a /validate that enforces structural rules."""

    def validate(self, plan_id: str) -> ToolResult:
        plan = self.plans.get(plan_id)
        if plan is None:
            return self._not_found(plan_id)
        errors = structural_errors(plan)
        if not errors:
            plan["status"] = "VALIDATED"
            return ToolResult(success=True, data={"valid": True, "errors": [], "status": "VALIDATED"})
        return ToolResult(
            success=True,
            data={"valid": False, "errors": errors, "status": plan["status"]},
        )


# ── scripted roles ───────────────────────────────────────────────────────────


class ScriptedPlanner:
    def __init__(self, patch: dict[str, Any]) -> None:
        self._patch = patch
        self.calls = 0

    async def draft(self, ctx: PlanContext) -> PlanPatch:
        self.calls += 1
        return PlanPatch(patch=copy.deepcopy(self._patch), rationale="scripted-draft")


class ScriptedCritic:
    def __init__(self, scores: list[float], issues: list[Issue] | None = None) -> None:
        self._scores = scores
        self._issues = issues or []
        self.calls = 0

    async def critique(self, plan: dict[str, Any]) -> PlanCritique:
        i = min(self.calls, len(self._scores) - 1)
        self.calls += 1
        score = self._scores[i]
        return PlanCritique(
            score=score,
            by_axis={"objective_fit": score, "targeting_coherence": score},
            issues=list(self._issues),
            summary="scripted-critique",
        )


class ScriptedRepairer:
    def __init__(self, patches: list[dict[str, Any] | None]) -> None:
        self._patches = patches
        self.calls = 0

    async def repair(self, plan, critique, violations) -> PlanPatch:
        i = min(self.calls, len(self._patches) - 1)
        self.calls += 1
        p = self._patches[i]
        return PlanPatch(patch=copy.deepcopy(p) if p else {}, rationale="scripted-repair")


# ── body fixtures (built from the shared per-slot payloads) ──────────────────

VALID_BODY = {
    "body": {
        "objective": OBJECTIVE,
        "budget": BUDGET,
        "schedule": SCHEDULE,
        "adGroups": AD_GROUPS,
        "creatives": CREATIVES,
    }
}
_OBJ_NO_PLATFORM = {k: v for k, v in OBJECTIVE.items() if k != "platformObjective"}
INVALID_BODY = {  # complete (all slots present) but structurally invalid
    "body": {
        "objective": _OBJ_NO_PLATFORM,
        "budget": BUDGET,
        "schedule": SCHEDULE,
        "adGroups": AD_GROUPS,
        "creatives": CREATIVES,
    }
}
FIX_OBJECTIVE = {"body": {"objective": {"platformObjective": "LEADS"}}}
REGRESS_OBJECTIVE = {"body": {"objective": {"platformObjective": None}}}  # deletes it
TWEAK = {"body": {"adGroups": AD_GROUPS}}  # re-set, no structural change (stays valid)


# ── plumbing ─────────────────────────────────────────────────────────────────


def _context(plan_id: str | None = None) -> dict[str, Any]:
    sc: dict[str, Any] = {}
    if plan_id:
        sc["plan_id"] = plan_id
    return {"headers": dict(AUTH_HEADERS), "session_context": sc}


async def _bootstrap(context: dict[str, Any]) -> str:
    """Create the DRAFT plan (name + productId) and return its id."""
    res = await create_plan.execute(
        {"name": SEED["name"], "product_id": SEED["productId"]}, context
    )
    assert res.success, res.error
    return context["session_context"]["plan_id"]


def _ctx(plan: dict[str, Any], fetched_ids: set[str] | None = None) -> PlanContext:
    return PlanContext(
        plan_id=plan.get("id"),
        plan=plan,
        vertical="real_estate",
        goal="site visits",
        fetched_ids=fetched_ids or set(),
    )


async def _run(planner, critic, repairer, *, draft_ids: set[str] | None = None):
    """Bootstrap a plan, run the generator, return (result, backend, planner,
    critic, repairer)."""
    backend = StructuralPlanBackend()
    context = _context()
    with PatchedSaasClient(backend):
        plan_id = await _bootstrap(context)
        io = PlanIO(context)
        plan = await io.get()
        gen = PlanGenerator(planner=planner, critic=critic, repairer=repairer)
        result = await gen.generate(_ctx(plan, draft_ids), io)
    return result, backend


def _obj(plan: dict[str, Any]) -> dict[str, Any]:
    return (plan.get("body") or {}).get("objective") or {}


# ── tests ──────────────────────────────────────────────────────────────────


class ConvergenceTests(unittest.TestCase):
    def test_converges_within_bound(self):
        planner = ScriptedPlanner(VALID_BODY)
        critic = ScriptedCritic([0.6, 0.9])  # first below, second clears 0.8
        repairer = ScriptedRepairer([TWEAK])
        result, _ = asyncio.run(_run(planner, critic, repairer))

        self.assertTrue(result.valid)
        self.assertTrue(result.converged)
        self.assertEqual(result.rounds, 1)
        self.assertAlmostEqual(result.score, 0.9)
        self.assertEqual(planner.calls, 1)
        self.assertEqual(repairer.calls, 1)
        self.assertEqual(critic.calls, 2)  # initial + after the accepted repair

    def test_immediate_convergence_no_repair(self):
        planner = ScriptedPlanner(VALID_BODY)
        critic = ScriptedCritic([0.95])
        repairer = ScriptedRepairer([TWEAK])
        result, _ = asyncio.run(_run(planner, critic, repairer))

        self.assertTrue(result.valid and result.converged)
        self.assertEqual(result.rounds, 0)
        self.assertEqual(repairer.calls, 0)  # already good — no repair fired


class ValidityFloorTests(unittest.TestCase):
    def test_repairs_invalid_draft_to_valid(self):
        planner = ScriptedPlanner(INVALID_BODY)  # missing objective.platformObjective
        critic = ScriptedCritic([0.9])
        repairer = ScriptedRepairer([FIX_OBJECTIVE])
        result, backend = asyncio.run(_run(planner, critic, repairer))

        self.assertTrue(result.valid, "invalid draft must be repaired to valid")
        self.assertTrue(result.converged)
        self.assertEqual(result.rounds, 1)
        self.assertEqual(repairer.calls, 1)
        # critic only runs once we are on the floor (not on the invalid draft).
        self.assertEqual(critic.calls, 1)
        self.assertEqual(_obj(result.plan).get("platformObjective"), "LEADS")

    def test_never_returns_invalid_when_unrepairable(self):
        # Repair never clears the structural violation -> we exhaust the bound
        # and MUST NOT claim a valid plan; violations are surfaced instead.
        planner = ScriptedPlanner(INVALID_BODY)
        critic = ScriptedCritic([0.9])
        repairer = ScriptedRepairer([TWEAK, TWEAK])  # never fixes platformObjective
        result, _ = asyncio.run(_run(planner, critic, repairer))

        self.assertFalse(result.valid)
        self.assertFalse(result.converged)
        self.assertLessEqual(result.rounds, MAX_REPAIR)
        self.assertTrue(any("platformObjective" in v for v in result.violations))


class BoundednessTests(unittest.TestCase):
    def test_non_convergence_returns_best_valid_and_is_bounded(self):
        # Hard case: valid throughout but the critic never clears the bar.
        planner = ScriptedPlanner(VALID_BODY)
        critic = ScriptedCritic(
            [0.5],
            issues=[Issue("body.adGroups[0].targeting", "warning",
                          "broaden the audience set", "targeting_coherence")],
        )
        repairer = ScriptedRepairer([TWEAK])  # keeps valid, never lifts the score
        result, _ = asyncio.run(_run(planner, critic, repairer))

        self.assertTrue(result.valid, "must return the best VALID draft, not invalid")
        self.assertFalse(result.converged)
        self.assertEqual(result.rounds, MAX_REPAIR, "must use the full bound, no more")
        self.assertLessEqual(repairer.calls, MAX_REPAIR)
        self.assertAlmostEqual(result.score, 0.5)
        # residual critique surfaced for A1 to act on
        self.assertIsNotNone(result.critique)
        self.assertTrue(result.critique.issues)


class MonotonicityTests(unittest.TestCase):
    def test_repair_regression_is_rolled_back(self):
        # Round 1 repair DELETES platformObjective (regresses validity); the loop
        # must roll back to the last valid state. Round 2 repair keeps it valid
        # and the second critique clears the bar.
        planner = ScriptedPlanner(VALID_BODY)
        critic = ScriptedCritic([0.5, 0.9])
        repairer = ScriptedRepairer([REGRESS_OBJECTIVE, TWEAK])
        result, backend = asyncio.run(_run(planner, critic, repairer))

        self.assertTrue(result.valid, "validity must never regress in the returned plan")
        self.assertEqual(result.rounds, 2)
        self.assertEqual(repairer.calls, 2)
        # The rollback restored platformObjective; the returned plan holds it.
        self.assertEqual(_obj(result.plan).get("platformObjective"), "LEADS")
        # Backend truth agrees: the persisted plan is structurally valid.
        self.assertEqual(structural_errors(list(backend.plans.values())[0]), [])


class GroundingTests(unittest.TestCase):
    def test_ground_patch_strips_unfetched_ids(self):
        patch = PlanPatch(patch={
            "body": {"adGroups": [{"targeting": {"audiences": {
                "customAudienceIds": ["aud_real", "aud_fake"],
                "interests": ["real_estate_investing"],  # text, not an id
            }}}]},
            "links": {"meta": {"pageId": "pg_fake", "adAccountId": "act_real"}},
        })
        grounded = ground_patch(patch, {"aud_real", "act_real"})

        aud = grounded.patch["body"]["adGroups"][0]["targeting"]["audiences"]
        self.assertEqual(aud["customAudienceIds"], ["aud_real"])
        self.assertEqual(aud["interests"], ["real_estate_investing"])  # untouched
        meta = grounded.patch["links"]["meta"]
        self.assertIsNone(meta["pageId"], "unfetched scalar id must be dropped (null)")
        self.assertEqual(meta["adAccountId"], "act_real")
        self.assertTrue(collect_ids(grounded.patch).issubset({"aud_real", "act_real"}))

    def test_empty_fetched_set_strips_all_ids(self):
        patch = PlanPatch(patch={"body": {"adGroups": [{"targeting": {"audiences": {
            "customAudienceIds": ["aud_1", "aud_2"]}}}]}})
        grounded = ground_patch(patch, set())
        aud = grounded.patch["body"]["adGroups"][0]["targeting"]["audiences"]
        self.assertEqual(aud["customAudienceIds"], [])

    def test_run_never_persists_unfetched_ids(self):
        # The draft emits one fetched + one unfetched audience id; the loop must
        # strip the unfetched one before it reaches the plan.
        body = copy.deepcopy(VALID_BODY)
        body["body"]["adGroups"][0]["targeting"]["audiences"] = {
            "customAudienceIds": ["aud_fetched", "aud_unfetched"],
            "interests": ["real_estate_investing"],
        }
        planner = ScriptedPlanner(body)
        critic = ScriptedCritic([0.95])
        repairer = ScriptedRepairer([TWEAK])
        result, backend = asyncio.run(
            _run(planner, critic, repairer, draft_ids={"aud_fetched"})
        )

        self.assertTrue(result.valid)
        persisted = collect_ids(list(backend.plans.values())[0])
        self.assertIn("aud_fetched", persisted)
        self.assertNotIn("aud_unfetched", persisted)


class DraftPlanToolTests(unittest.TestCase):
    """The draft_plan ToolDefinition — wiring, validation, and the no-plan guard."""

    def _scripted_generator(self, planner, critic, repairer) -> PlanGenerator:
        return PlanGenerator(planner=planner, critic=critic, repairer=repairer)

    def test_tool_end_to_end_whole_plan(self):
        planner = ScriptedPlanner(VALID_BODY)
        critic = ScriptedCritic([0.9])
        repairer = ScriptedRepairer([TWEAK])
        gen = self._scripted_generator(planner, critic, repairer)

        async def _go() -> ToolResult:
            backend = StructuralPlanBackend()
            context = _context()
            with PatchedSaasClient(backend):
                await _bootstrap(context)
                with mock.patch.object(loop_mod, "get_plan_generator", lambda: gen):
                    return await draft_plan.execute(
                        {"mode": "whole", "goal": "site visits"}, context
                    ), context

        result, context = asyncio.run(_go())
        self.assertTrue(result.success, result.error)
        self.assertTrue(result.data["valid"])
        self.assertTrue(result.data["converged"])
        # residual critique stashed for the rail
        self.assertIn("plan_critique", context["session_context"])

    def test_tool_requires_active_plan(self):
        async def _go() -> ToolResult:
            backend = StructuralPlanBackend()
            with PatchedSaasClient(backend):
                return await draft_plan.execute({"mode": "whole"}, _context())

        result = asyncio.run(_go())
        self.assertFalse(result.success)
        self.assertIn("create_plan", result.error)

    def test_tool_section_mode_needs_section(self):
        async def _go() -> ToolResult:
            backend = StructuralPlanBackend()
            context = _context()
            with PatchedSaasClient(backend):
                await _bootstrap(context)
                return await draft_plan.execute({"mode": "section"}, context)

        result = asyncio.run(_go())
        self.assertFalse(result.success)
        self.assertIn("section", result.error.lower())

    def test_tool_reports_invalid_as_failure(self):
        # Unrepairable invalid plan -> the tool must not report success.
        planner = ScriptedPlanner(INVALID_BODY)
        critic = ScriptedCritic([0.9])
        repairer = ScriptedRepairer([TWEAK, TWEAK])
        gen = self._scripted_generator(planner, critic, repairer)

        async def _go() -> ToolResult:
            backend = StructuralPlanBackend()
            context = _context()
            with PatchedSaasClient(backend):
                await _bootstrap(context)
                with mock.patch.object(loop_mod, "get_plan_generator", lambda: gen):
                    return await draft_plan.execute({"mode": "whole"}, context)

        result = asyncio.run(_go())
        self.assertFalse(result.success)
        self.assertFalse(result.data["valid"])


class ConstantsTests(unittest.TestCase):
    def test_bound_and_threshold(self):
        self.assertEqual(MAX_REPAIR, 2)
        self.assertGreater(DEFAULT_THRESHOLD, 0.0)
        self.assertLessEqual(DEFAULT_THRESHOLD, 1.0)


if __name__ == "__main__":
    unittest.main()
