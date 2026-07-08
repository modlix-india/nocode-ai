"""Adzump2 P0 offline eval harness — DETERMINISTIC: no network, no LLM, no DB.

Drives the real adzump2 plan TOOLS (create_plan / get_plan / update_plan /
get_completeness / validate_plan) and the agent's completeness RAIL
(build_turn_reminder) against an in-memory FakePlanBackend that mirrors the
Java adzump service's P0 semantics:

  POST  /api/adzump/plans                    create (DRAFT, server-managed id/revision)
  GET   /api/adzump/plans/{id}               read
  PATCH /api/adzump/plans/{id}               RFC-7386 merge patch (incl. over `body`)
  GET   /api/adzump/plans/{id}/completeness  derived required-slot status
  POST  /api/adzump/plans/{id}/validate      structural verdict (complete → VALIDATED)

Required P0 slots (mirror of the Java completeness rules): name, productId,
objective, budget, schedule, adGroups-or-assetGroups, creatives.

The SaasClient is patched at the CLASS level (SaasClient._request) so every
/api/adzump/* call from any tool routes to the fake; anything else is blocked
with a clean error (proof the harness is offline).

Each SCENARIO is a scripted tool-call sequence; the scorecard reports steps
run, completeness progression, dead ends (a step that neither errors-cleanly
nor progresses), and the final complete/valid booleans. Exit code 1 if any
scenario fails.

Run:
    cd nocode-ai && ./venv/bin/python -m scripts.adzump2.eval

TODO(P1): turn-level LLM-in-the-loop eval — feed scripted USER turns through
Adzump2Agent.run() with a ScriptedProvider (canned tool_use choices), so the
prompt + rail steering is scored too, not just the tool layer. This harness
deliberately stays below the model: cheapest-layer probing first
(tests/agents/adzump/test_adversarial_probe.py pattern), rubric direction in
app/learning/outcome.py.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import sys
from dataclasses import dataclass, field
from typing import Any

logging.basicConfig(level=logging.WARNING)

from app.config import settings

# Provider-key checks must never bite an offline run (set BEFORE agent import).
for _key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "MINIMAX_API_KEY"):
    if not getattr(settings, _key, ""):
        setattr(settings, _key, "offline-eval-key")

from app.core.session import AuthContext, BaseSession
from app.core.tools.base import ToolDefinition, ToolResult
from app.core.tools.http_client import SaasClient
from app.agents.adzump2.agent import Adzump2Agent
from app.agents.adzump2.diagnose.diagnose import MAX_LLM_CALLS, get_diagnose_agent
from app.agents.adzump2.tools.registry import ALL_TOOLS

from tests.agents.adzump2.fixtures import (
    BUDGET_PATCH,
    BUDGET_SCHEDULE_PATCH,
    DIAG_ACTION_SET,
    DIAG_ATTRIBUTE_GAPS,
    DIAG_ATTRIBUTE_MAP,
    DIAG_LLM_DIAGNOSIS,
    DIAG_SNAPSHOT,
    GROUPS_CREATIVES_PATCH,
    OBJECTIVE_PATCH,
    OBJECTIVE_SCHEDULE_PATCH,
    REQUIRED_SLOTS,
)

# ── FakePlanBackend ────────────────────────────────────────────────────────

SERVER_FIELDS = {"id", "revision", "clientCode", "status", "schemaVersion"}


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386 JSON merge patch: present keys overwrite, null deletes,
    absent keys untouched; a non-object patch replaces wholesale."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result: dict[str, Any] = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def derive_completeness(plan: dict[str, Any]) -> dict[str, Any]:
    """Mirror of the Java P0 completeness derivation (never stored)."""
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
    filled = [s for s in REQUIRED_SLOTS if checks[s]]
    missing = [s for s in REQUIRED_SLOTS if not checks[s]]
    return {
        "complete": not missing,
        "missingRequired": missing,
        "filled": filled,
        "requiredSlots": list(REQUIRED_SLOTS),
    }


class FakePlanBackend:
    """In-memory stand-in for the Java adzump plan service (P0 endpoints)."""

    def __init__(self) -> None:
        self.plans: dict[str, dict[str, Any]] = {}
        self._seq = 0

    # -- endpoint semantics ------------------------------------------------

    def create(self, seed: dict[str, Any] | None, headers: dict[str, str]) -> ToolResult:
        self._seq += 1
        plan: dict[str, Any] = {
            "schemaVersion": "1.0",
            "id": f"cp_EVAL{self._seq:04d}",
            "revision": 1,
            "clientCode": headers.get("clientCode", "SYSTEM"),
            "status": "DRAFT",
            "name": None,
            "productId": None,
            "body": {},
        }
        if seed:
            clean = {k: v for k, v in seed.items() if k not in SERVER_FIELDS}
            plan = merge_patch(plan, clean)
        self.plans[plan["id"]] = plan
        return ToolResult(success=True, data=self._with_completeness(plan),
                          summary=f"POST /api/adzump/plans → 200 ({plan['id']})")

    def get(self, plan_id: str) -> ToolResult:
        plan = self.plans.get(plan_id)
        if plan is None:
            return self._not_found(plan_id)
        return ToolResult(success=True, data=self._with_completeness(plan))

    def patch(self, plan_id: str, patch: dict[str, Any] | None) -> ToolResult:
        plan = self.plans.get(plan_id)
        if plan is None:
            return self._not_found(plan_id)
        if not isinstance(patch, dict) or not patch:
            return ToolResult(success=False, error="HTTP 400: merge patch body required")
        clean = {k: v for k, v in patch.items() if k not in SERVER_FIELDS}
        merged = merge_patch(plan, clean)
        merged["revision"] = plan["revision"] + 1
        self.plans[plan_id] = merged
        return ToolResult(success=True, data=self._with_completeness(merged),
                          summary=f"PATCH /api/adzump/plans/{plan_id} → 200 (rev {merged['revision']})")

    def completeness(self, plan_id: str) -> ToolResult:
        plan = self.plans.get(plan_id)
        if plan is None:
            return self._not_found(plan_id)
        return ToolResult(success=True, data=derive_completeness(plan))

    def validate(self, plan_id: str) -> ToolResult:
        plan = self.plans.get(plan_id)
        if plan is None:
            return self._not_found(plan_id)
        comp = derive_completeness(plan)
        if comp["complete"]:
            plan["status"] = "VALIDATED"
            verdict = {"valid": True, "errors": [], "status": "VALIDATED"}
        else:
            verdict = {
                "valid": False,
                "errors": [f"missing required slot: {s}" for s in comp["missingRequired"]],
                "status": plan["status"],
            }
        return ToolResult(success=True, data=verdict)

    # -- HTTP-shaped router (what the patched SaasClient calls) -------------

    def handle(self, method: str, path: str, body: Any,
               headers: dict[str, str]) -> ToolResult:
        path = path if path.startswith("/") else f"/{path}"
        path = path.rstrip("/")
        # A5 read/recommend surface (J10/J12/J20 + propose) — served from seeds.
        a5 = re.fullmatch(
            r"/api/adzump/plans/([^/]+)/(performance|recommendations|attribute-map|actions/propose)",
            path,
        )
        if a5:
            return self._a5(method, a5.group(2), body)
        m = re.fullmatch(r"/api/adzump/plans(?:/([^/]+))?(?:/(completeness|validate))?", path)
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

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _not_found(plan_id: str) -> ToolResult:
        return ToolResult(success=False, error=f"HTTP 404: Plan not found: {plan_id}")

    @staticmethod
    def _with_completeness(plan: dict[str, Any]) -> dict[str, Any]:
        out = copy.deepcopy(plan)
        out["completeness"] = derive_completeness(plan)
        return out

    # -- A5 read/recommend surface (served from the seeded fixtures) --------

    @staticmethod
    def _a5(method: str, sub: str, body: Any) -> ToolResult:
        """J10 snapshot / J12 ActionSet / J20 map (GET) + propose (POST). The
        propose gate ACCEPTS and requires approval — it applies nothing (P3)."""
        seeds = {
            "performance": DIAG_SNAPSHOT,
            "recommendations": DIAG_ACTION_SET,
            "attribute-map": DIAG_ATTRIBUTE_MAP,
        }
        if method == "GET" and sub in seeds:
            return ToolResult(success=True, data=copy.deepcopy(seeds[sub]))
        if method == "POST" and sub == "actions/propose":
            b = body if isinstance(body, dict) else {}
            return ToolResult(success=True, data={
                "type": b.get("type"), "targetId": b.get("targetId"),
                "change": b.get("change") or {}, "rationale": b.get("rationale") or "",
                "significanceVerdict": "SIGNIFICANT", "risk": "LOW", "requiresApproval": True,
            })
        return ToolResult(success=False, error=f"HTTP 405: {method} /{sub}")


class PatchedSaasClient:
    """Context manager: route SaasClient traffic to the FakePlanBackend.

    Patches ``SaasClient._request`` on the CLASS so every instance (module
    singleton, lazy factory, per-call construction) is intercepted. Non-adzump
    paths get a clean error — the harness must never touch the network.
    """

    def __init__(self, backend: FakePlanBackend) -> None:
        self.backend = backend
        self._original = None

    def __enter__(self) -> "PatchedSaasClient":
        backend = self.backend

        async def _request(_self: SaasClient, method: str, path: str,
                           headers: dict | None = None, json: Any = None,
                           params: dict | None = None) -> ToolResult:
            url = path if path.startswith("/") else f"/{path}"
            if url.startswith("/api/adzump/"):
                return backend.handle(method, url, json, headers or {})
            return ToolResult(
                success=False,
                error=f"offline-eval: blocked non-adzump call {method} {url}",
            )

        self._original = SaasClient._request
        SaasClient._request = _request
        return self

    def __exit__(self, *exc: Any) -> None:
        SaasClient._request = self._original


# ── scripted scenarios ─────────────────────────────────────────────────────

SEED = {"name": "Whitefield Launch - Site Visits", "productId": "prd_5521"}


@dataclass
class Step:
    """One scripted tool call.

    expect:
      progress    — must succeed AND reduce the missing-slot count (or create the plan)
      read        — must succeed (no progress required)
      complete    — must succeed AND report/stash complete=True
      valid       — must succeed AND report valid=True
      clean_error — must FAIL as a ToolResult (error text, no exception)
    """

    tool: str
    intent: str
    expect: str
    patch: dict[str, Any] | None = None
    seed: dict[str, Any] | None = None
    use_plan_id: bool = True


@dataclass
class Scenario:
    name: str
    description: str
    steps: list[Step] = field(default_factory=list)


SCENARIOS: list[Scenario] = [
    Scenario(
        name="happy-build",
        description="create → objective → budget+schedule → adGroups+creatives → complete → valid",
        steps=[
            Step("create_plan", "create draft plan", "progress", seed=SEED, use_plan_id=False),
            Step("update_plan", "set objective", "progress", patch=OBJECTIVE_PATCH),
            Step("update_plan", "set budget + schedule", "progress", patch=BUDGET_SCHEDULE_PATCH),
            Step("update_plan", "set adGroups + creatives", "progress", patch=GROUPS_CREATIVES_PATCH),
            Step("get_completeness", "completeness check", "complete"),
            Step("validate_plan", "structural validate", "valid"),
        ],
    ),
    Scenario(
        name="out-of-order",
        description="budget lands before objective; the build still converges",
        steps=[
            Step("create_plan", "create draft plan", "progress", seed=SEED, use_plan_id=False),
            Step("update_plan", "set budget FIRST", "progress", patch=BUDGET_PATCH),
            Step("update_plan", "set objective + schedule", "progress", patch=OBJECTIVE_SCHEDULE_PATCH),
            Step("update_plan", "set adGroups + creatives", "progress", patch=GROUPS_CREATIVES_PATCH),
            Step("get_completeness", "completeness check", "complete"),
            Step("validate_plan", "structural validate", "valid"),
        ],
    ),
    Scenario(
        name="no-plan",
        description="update_plan with no plan created must fail cleanly, not raise",
        steps=[
            Step("update_plan", "update without create", "clean_error",
                 patch=OBJECTIVE_PATCH, use_plan_id=False),
        ],
    ),
]


# ── harness plumbing ───────────────────────────────────────────────────────

def fill_params(tool: ToolDefinition, *, plan_id: str | None = None,
                patch: dict | None = None, seed: dict | None = None) -> dict[str, Any]:
    """Adapt scripted intent to the tool's declared parameter names (the
    adzump2 package owns the exact names; map by shape, same rules as
    tests/agents/adzump2/test_agent.py)."""
    params: dict[str, Any] = {}
    object_params = [p for p in tool.parameters if p.type == "object"]
    for p in tool.parameters:
        low = p.name.lower()
        if p.type != "string":
            continue
        if plan_id is not None and ("plan" in low or low in ("id", "planid", "plan_id")):
            params[p.name] = plan_id
        elif seed and "name" in low:
            params[p.name] = seed.get("name")
        elif seed and "product" in low:
            params[p.name] = seed.get("productId")
    payload = patch if patch is not None else seed
    if payload is not None and object_params:
        params[object_params[0].name] = payload
    return params


def _find_plan_id(data: Any) -> str | None:
    """Locate the backend-issued plan id (cp_*) anywhere in a result payload."""
    if isinstance(data, dict):
        for key in ("id", "planId", "plan_id"):
            value = data.get(key)
            if isinstance(value, str) and value.startswith("cp_"):
                return value
        for value in data.values():
            found = _find_plan_id(value)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _find_plan_id(value)
            if found:
                return found
    return None


def _find_key(data: Any, key: str) -> Any:
    """First value under `key` anywhere in a nested payload (None if absent)."""
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for value in data.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _find_key(value, key)
            if found is not None:
                return found
    return None


def _slot_words(slot: str) -> list[str]:
    """Match forms for a slot in rail prose: 'adGroups' → ['adgroups', 'ad groups']."""
    spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", slot).lower()
    return list({slot.lower(), spaced})


def _make_session(name: str) -> BaseSession:
    """Real BaseSession wired with offline auth; no DB (get_or_create skipped)."""
    session = BaseSession(agent_name="adzump2")
    session.session_id = f"SYSTEM_eval_{name}"
    session.auth = AuthContext(
        token="offline-eval-token",
        client_code="SYSTEM",
        client_id=0,
        user_id=0,
        app_code="adzump",
        access_app_code="adzump",
    )
    return session


@dataclass
class StepReport:
    step: Step
    ok: bool
    missing_after: list[str] | None
    note: str = ""
    dead_end: bool = False


@dataclass
class ScenarioReport:
    scenario: Scenario
    steps: list[StepReport] = field(default_factory=list)
    rail_notes: list[str] = field(default_factory=list)
    final_complete: bool | None = None
    final_valid: bool | None = None

    @property
    def dead_ends(self) -> int:
        return sum(1 for s in self.steps if s.dead_end)

    @property
    def rail_ok(self) -> bool:
        return not self.rail_notes

    @property
    def passed(self) -> bool:
        return all(s.ok for s in self.steps) and self.dead_ends == 0 and self.rail_ok

    @property
    def progression(self) -> str:
        counts: list[str] = []
        for s in self.steps:
            counts.append("-" if s.missing_after is None else str(len(s.missing_after)))
        return "→".join(counts)  # arrow-joined missing-slot counts


async def run_scenario(agent: Adzump2Agent, scenario: Scenario) -> ScenarioReport:
    backend = FakePlanBackend()
    session = _make_session(scenario.name)
    tools = {t.name: t for t in ALL_TOOLS}
    report = ScenarioReport(scenario=scenario)
    plan_id: str | None = None

    with PatchedSaasClient(backend):
        # Rail check 0: with no plan, the rail must steer to creating one.
        rail = await agent.build_turn_reminder(session, 1)
        if "create" not in (rail or "").lower():
            report.rail_notes.append("no-plan rail does not steer to create")

        for step in scenario.steps:
            tool = tools.get(step.tool)
            if tool is None or tool.execute is None:
                report.steps.append(StepReport(step, False, None, note="tool missing from registry"))
                continue

            missing_before = (
                len(derive_completeness(backend.plans[plan_id])["missingRequired"])
                if plan_id and plan_id in backend.plans else None
            )
            params = fill_params(
                tool,
                plan_id=plan_id if step.use_plan_id else None,
                patch=step.patch,
                seed=step.seed,
            )
            context = agent.build_tool_context(session)

            try:
                result = await tool.execute(params, context)
            except Exception as e:  # a tool must NEVER raise — clean errors only
                report.steps.append(StepReport(
                    step, False, None, dead_end=True,
                    note=f"RAISED {type(e).__name__}: {e}",
                ))
                continue

            # Adopt the created plan id; note if the tool forgot to stash it.
            note = ""
            if step.tool == "create_plan" and result.success:
                plan_id = session.context.get("plan_id") or _find_plan_id(result.data)
                if not session.context.get("plan_id"):
                    note = "create_plan did not stash plan_id (harness set it)"
                    if plan_id:
                        session.context["plan_id"] = plan_id

            missing_after = (
                derive_completeness(backend.plans[plan_id])["missingRequired"]
                if plan_id and plan_id in backend.plans else None
            )
            progressed = (
                (missing_before is None and missing_after is not None)
                or (missing_before is not None and missing_after is not None
                    and len(missing_after) < missing_before)
            )

            if step.expect == "clean_error":
                ok = (not result.success) and bool(result.error)
                if not ok:
                    note = note or f"expected clean error, got success={result.success}"
            elif step.expect == "progress":
                ok = result.success and progressed
                if result.success and not progressed:
                    note = note or "no progress (dead end)"
            elif step.expect == "complete":
                stashed = session.context.get("plan_completeness") or {}
                ok = result.success and bool(stashed.get("complete"))
                if result.success and not ok:
                    note = note or f"completeness not stashed/complete: {stashed}"
            elif step.expect == "valid":
                ok = result.success and _find_key(result.data, "valid") is True
                if result.success and not ok:
                    note = note or f"no valid=true verdict in: {result.data}"
            else:  # read
                ok = result.success

            dead_end = (step.expect == "progress" and result.success and not progressed)
            if not result.success and step.expect != "clean_error":
                note = note or f"clean error: {result.error}"
            report.steps.append(StepReport(step, ok, missing_after, note=note, dead_end=dead_end))

            # Contract: after a successful update_plan the stash must mirror truth.
            if step.tool in ("update_plan", "get_completeness") and result.success and plan_id:
                stashed = session.context.get("plan_completeness")
                truth = derive_completeness(backend.plans[plan_id])
                if not isinstance(stashed, dict):
                    report.rail_notes.append(f"{step.tool} did not stash plan_completeness")
                elif stashed.get("missingRequired") != truth["missingRequired"]:
                    report.rail_notes.append(
                        f"{step.tool} stash drifted from backend truth: "
                        f"{stashed.get('missingRequired')} != {truth['missingRequired']}"
                    )

            # Rail check per update step: incomplete → names a missing slot;
            # complete → steers to validate. (Not after create_plan — it stashes
            # only plan_id, so the rail legitimately reads "completeness unknown".)
            if step.tool == "update_plan" and result.success and plan_id:
                rail = (await agent.build_turn_reminder(session, 1)) or ""
                low = rail.lower()
                truth = derive_completeness(backend.plans[plan_id])
                if truth["missingRequired"]:
                    named = any(
                        w in low for s in truth["missingRequired"] for w in _slot_words(s)
                    )
                    if not named:
                        report.rail_notes.append(
                            f"rail after '{step.intent}' names none of {truth['missingRequired']}"
                        )
                elif "validate" not in low:
                    report.rail_notes.append("rail after completion does not steer to validate")

        if plan_id and plan_id in backend.plans:
            final = backend.plans[plan_id]
            report.final_complete = derive_completeness(final)["complete"]
            report.final_valid = final.get("status") == "VALIDATED"

    return report


# ── scorecard ──────────────────────────────────────────────────────────────

def _print_scenario(report: ScenarioReport) -> None:
    sc = report.scenario
    print(f"\n== {sc.name} :: {sc.description}")
    print(f"   {'#':<2} {'tool':<18} {'intent':<28} {'ok':<5} {'missing':<9} note")
    for i, sr in enumerate(report.steps, 1):
        missing = "-" if sr.missing_after is None else str(len(sr.missing_after))
        flag = "OK" if sr.ok else ("DEAD" if sr.dead_end else "FAIL")
        print(f"   {i:<2} {sr.step.tool:<18} {sr.step.intent:<28} {flag:<5} {missing:<9} {sr.note}")
    for note in report.rail_notes:
        print(f"   rail: {note}")


def _print_summary(reports: list[ScenarioReport]) -> None:
    print("\n" + "=" * 96)
    header = (f"{'scenario':<14} {'steps':<6} {'progression':<16} {'dead_ends':<10} "
              f"{'complete':<9} {'valid':<6} {'rail':<5} result")
    print(header)
    print("-" * 96)
    for r in reports:
        comp = "-" if r.final_complete is None else str(r.final_complete)
        valid = "-" if r.final_valid is None else str(r.final_valid)
        rail = "ok" if r.rail_ok else "FAIL"
        result = "PASS" if r.passed else "FAIL"
        print(f"{r.scenario.name:<14} {len(r.steps):<6} {r.progression:<16} "
              f"{r.dead_ends:<10} {comp:<9} {valid:<6} {rail:<5} {result}")
    print("=" * 96)


# ── A5 diagnose scenario (offline: seam monkeypatched, SaasClient faked) ─────

@dataclass
class DiagnoseReport:
    name: str = "diagnose"
    description: str = "read J10+J12+J20 → narrate + prioritize + watchlist + grounded tests"
    checks: list[tuple[str, bool]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(ok for _, ok in self.checks)


async def run_diagnose_scenario() -> DiagnoseReport:
    """Drive the real ``diagnose`` + ``propose_action`` tools against the seeded
    A5 read surface, with the DiagnoseAgent's LLM seam monkeypatched to a canned
    Diagnosis. Asserts A5's discipline: narrate + prioritize J12 (verbatim
    numbers), watchlist thin grains (never act-now), ground tests in real gaps,
    bounded LLM calls, and apply nothing."""
    backend = FakePlanBackend()
    report = DiagnoseReport()
    tools = {t.name: t for t in ALL_TOOLS}
    ctx: dict[str, Any] = {
        "session_context": {"plan_id": "cp_EVAL_DIAG"},
        "headers": {"clientCode": "SYSTEM"},
    }

    counter: dict[str, int] = {}

    async def seam(task: str, *, purpose: str, auth=None, event_stream=None):
        counter[purpose] = counter.get(purpose, 0) + 1
        return copy.deepcopy(DIAG_LLM_DIAGNOSIS)

    singleton = get_diagnose_agent()
    original = singleton._llm_json
    singleton._llm_json = seam  # type: ignore[assignment]
    try:
        with PatchedSaasClient(backend):
            res = await tools["diagnose"].execute({"window": "30d"}, ctx)
            data = res.data if isinstance(res.data, dict) else {}
            sig = data.get("signals", {})
            watch_ids = {w["targetId"] for w in data.get("watchlist", [])}
            ranked = data.get("rankedActions", [])
            ranked_ids = {a["targetId"] for a in ranked}
            tests = data.get("testProposals", [])
            shift = next((a for a in ranked if a["type"] == "SHIFT_BUDGET"), {})

            report.checks.append(("diagnose succeeds", res.success))
            report.checks.append((
                "winning angle investment_roi located",
                any(w["value"] == "investment_roi" for w in sig.get("winningAttributes", [])),
            ))
            report.checks.append((
                "junk source adset_broad located",
                bool(sig.get("junkSources")) and sig["junkSources"][0]["targetId"] == "adset_broad",
            ))
            report.checks.append(("thin grain adset_new on watchlist", "adset_new" in watch_ids))
            report.checks.append(("thin grain NOT ranked act-now", "adset_new" not in ranked_ids))
            report.checks.append(("ranked ⊆ J12 (2 gated actions)", len(ranked) == 2))
            report.checks.append(("J12 numbers verbatim (expectedDelta 4.2)",
                                  shift.get("expectedDelta") == 4.2))
            report.checks.append((
                "test proposals grounded in real J20 gaps",
                len(tests) == 2 and all(t["grounded"] for t in tests)
                and all((t["groundsOn"]["axis"], t["groundsOn"]["value"]) in DIAG_ATTRIBUTE_GAPS
                        for t in tests),
            ))
            report.checks.append(("bounded LLM calls (1, no thrash)",
                                  data.get("llmCalls") == 1 and counter.get("diagnose") == 1
                                  and counter.get("diagnose", 0) <= MAX_LLM_CALLS))

            pres = await tools["propose_action"].execute(
                {"type": "REFINE_AUDIENCE", "target_id": "adset_roi",
                 "change": {"expand": ["nri_investors"]}, "rationale": "scale the winner"},
                ctx,
            )
            report.checks.append(("propose_action routes through the J12 gate",
                                  pres.success and "gate" in pres.summary.lower()))
    finally:
        singleton._llm_json = original  # type: ignore[assignment]
    return report


def _print_diagnose(report: DiagnoseReport) -> None:
    print(f"\n== {report.name} :: {report.description}")
    for label, ok in report.checks:
        print(f"   {'OK' if ok else 'FAIL':<5} {label}")


async def main() -> int:
    print("adzump2 offline eval — deterministic tool + rail harness (no network, no LLM)")
    agent = Adzump2Agent.get_instance()
    reports: list[ScenarioReport] = []
    for scenario in SCENARIOS:
        report = await run_scenario(agent, scenario)
        reports.append(report)
        _print_scenario(report)
    _print_summary(reports)

    diag = await run_diagnose_scenario()
    _print_diagnose(diag)

    failed = [r.scenario.name for r in reports if not r.passed]
    if not diag.passed:
        failed.append(diag.name)
    if failed:
        print(f"\nFAILED scenarios: {', '.join(failed)}")
        return 1
    print("\nAll scenarios passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
