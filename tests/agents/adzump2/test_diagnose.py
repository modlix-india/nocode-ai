"""Unit: app/agents/adzump2/diagnose (A5) — offline, no network, no LLM, no DB.

The single LLM seam (``DiagnoseAgent._llm_json``) is monkeypatched with a canned
raw model ``Diagnosis`` (fixtures.DIAG_LLM_DIAGNOSIS), and the SaasClient is faked
so the whole read → reason → emit pipeline + the ``diagnose`` / ``propose_action``
tools are provable with no live model and no gateway.

Covers (A5-diagnose.md §8):
  (a) the diagnosis identifies the winning angle (investment_roi) and LOCATES the
      junk source (adset_broad) — deterministic ``signals`` + narrative
  (b) a thin / FAST_ONLY grain (adset_new) lands on the WATCHLIST and is NEVER in
      ranked act-now actions — even though the canned model tried to PAUSE it
  (c) ranked_actions is a strict SUBSET of the J12 ActionSet and carries J12's
      numbers VERBATIM (A5 recomputes nothing)
  (d) test_proposals are kept ONLY when grounded in a REAL gap in the J20 map
      (the "double down on the winner" proposal is dropped)
  (e) the seam is called a BOUNDED number of times (no M3 thrash), and the
      pipeline degrades cleanly when the model returns nothing
  (f) the diagnose tool runs offline (reads the 3 contract endpoints, applies
      nothing) and propose_action routes a new action through the gate

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump2.test_diagnose -v
"""

from __future__ import annotations

import asyncio
import copy
import unittest
from typing import Any
from unittest import mock

from app.config import settings

# Provider-key checks must never bite an offline unit test (set before import).
for _key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "MINIMAX_API_KEY"):
    if not getattr(settings, _key, ""):
        setattr(settings, _key, "offline-test-key")

from app.agents.adzump2.diagnose.diagnose import (
    MAX_LLM_CALLS,
    DiagnoseAgent,
    get_diagnose_agent,
)
from app.agents.adzump2.diagnose.models import Diagnosis
from app.agents.adzump2.diagnose.tools import diagnose, propose_action
from app.core.tools.base import ToolResult
from tests.agents.adzump2.fixtures import (
    DIAG_ACTION_SET,
    DIAG_ATTRIBUTE_GAPS,
    DIAG_ATTRIBUTE_MAP,
    DIAG_LLM_DIAGNOSIS,
    DIAG_SNAPSHOT,
)

_PLAN_ID = "cp_01HTEST0001"
_BASE = f"/api/adzump/plans/{_PLAN_ID}"


# ── scripted LLM seam (mirrors test_creative._scripted_llm) ───────────────────


def _scripted_llm(payload: Any, counter: dict[str, int]):
    """Fake ``_llm_json`` seam returning a deep copy of ``payload`` (or, if it is
    a callable, ``payload(call_index)``), counting calls per purpose."""

    async def fake(task: str, *, purpose: str, auth=None, event_stream=None):
        counter[purpose] = counter.get(purpose, 0) + 1
        if callable(payload):
            return payload(counter[purpose] - 1)
        return copy.deepcopy(payload)

    return fake


def _fresh_agent(payload: Any, counter: dict[str, int]) -> DiagnoseAgent:
    """A non-singleton DiagnoseAgent with the LLM seam scripted (dies per test)."""
    agent = DiagnoseAgent()
    agent._llm_json = _scripted_llm(payload, counter)  # type: ignore[assignment]
    return agent


def _run_diagnose(payload: Any = DIAG_LLM_DIAGNOSIS) -> tuple[Diagnosis, dict[str, int]]:
    counter: dict[str, int] = {}
    agent = _fresh_agent(payload, counter)
    diagnosis = asyncio.run(
        agent.diagnose(
            snapshot=DIAG_SNAPSHOT,
            action_set=DIAG_ACTION_SET,
            attribute_map=DIAG_ATTRIBUTE_MAP,
            vertical="real_estate",
        )
    )
    return diagnosis, counter


# ── the diagnose() engine ─────────────────────────────────────────────────────


class DiagnoseEngineTests(unittest.TestCase):
    def test_identifies_winning_angle_and_locates_junk_source(self) -> None:
        d, _ = _run_diagnose()
        self.assertIsInstance(d, Diagnosis)
        # winning attribute located (exploit): investment_roi is a proven winner.
        win_values = {w["value"] for w in d.signals["winningAttributes"]}
        self.assertIn("investment_roi", win_values)
        self.assertIn("nri_investors", win_values)
        # junk source located: the broad-keyword ad set, ranked first by junk rate.
        self.assertTrue(d.signals["junkSources"])
        self.assertEqual(d.signals["junkSources"][0]["targetId"], "adset_broad")
        # and the narrative names both.
        self.assertIn("investment_roi", d.narrative)
        self.assertIn("adset_broad", d.narrative)

    def test_thin_grain_watchlisted_and_never_ranked(self) -> None:
        d, _ = _run_diagnose()
        # the FAST_ONLY grain is detected as thin ...
        self.assertIn("adset_new", d.signals["thinGrains"])
        # ... lands on the watchlist ...
        watch_ids = {w.target_id for w in d.watchlist}
        self.assertIn("adset_new", watch_ids)
        new_item = next(w for w in d.watchlist if w.target_id == "adset_new")
        self.assertEqual(new_item.signal_maturity, "FAST_ONLY")
        # ... and is NEVER an act-now action, even though the model tried to PAUSE it.
        ranked_ids = {a.target_id for a in d.ranked_actions}
        self.assertNotIn("adset_new", ranked_ids)
        self.assertFalse(any(a.type == "PAUSE_ENTITY" for a in d.ranked_actions))
        # the drop is explained (fast signal proposes, slow signal disposes).
        self.assertTrue(any("adset_new" in w and "watchlist" in w for w in d.warnings))

    def test_ranked_actions_subset_of_j12_with_verbatim_numbers(self) -> None:
        d, _ = _run_diagnose()
        j12_keys = {(a["targetId"], a["type"]) for a in DIAG_ACTION_SET["actions"]}
        # A5 narrates ONLY gated J12 actions — never invents an act-now.
        self.assertEqual(len(d.ranked_actions), 2)
        for a in d.ranked_actions:
            self.assertIn((a.target_id, a.type), j12_keys)
        # priority ordering came from the model (SHIFT first, NEGKW second).
        self.assertEqual(d.ranked_actions[0].type, "SHIFT_BUDGET")
        self.assertEqual(d.ranked_actions[1].type, "ADD_NEGATIVE_KEYWORD")
        # numbers + verdicts are J12's, VERBATIM (A5 recomputes nothing).
        shift = d.ranked_actions[0]
        src = next(a for a in DIAG_ACTION_SET["actions"] if a["type"] == "SHIFT_BUDGET")
        self.assertEqual(shift.expected_delta, src["expectedDelta"])
        self.assertEqual(shift.confidence, src["confidence"])
        self.assertEqual(shift.significance_verdict, src["significanceVerdict"])
        self.assertEqual(shift.risk, src["risk"])
        self.assertTrue(shift.requires_approval)
        self.assertEqual(shift.change, src["change"])
        # the model added the business "why"; the J12 rationale is preserved too.
        self.assertTrue(shift.why)
        self.assertEqual(shift.rationale, src["rationale"])

    def test_test_proposals_map_to_real_attribute_gaps(self) -> None:
        d, _ = _run_diagnose()
        # the "double down on investment_roi" proposal (a WINNER, not a gap) is dropped.
        self.assertEqual(len(d.test_proposals), 2)
        for t in d.test_proposals:
            self.assertTrue(t.grounded)
            key = (t.grounds_on["axis"], t.grounds_on["value"])
            self.assertIn(key, DIAG_ATTRIBUTE_GAPS)
        grounded_values = {t.grounds_on["value"] for t in d.test_proposals}
        self.assertIn("possession_ready", grounded_values)
        self.assertIn("low_price_band", grounded_values)
        self.assertNotIn("investment_roi", grounded_values)
        # the ungrounded proposal drop is explained.
        self.assertTrue(any("ungrounded test proposal" in w for w in d.warnings))

    def test_recomputes_nothing_and_bounded_llm_calls(self) -> None:
        d, counter = _run_diagnose()
        # ONE unified diagnose pass on the happy path; hard cap otherwise.
        self.assertEqual(counter.get("diagnose"), 1)
        self.assertEqual(d.llm_calls, 1)
        self.assertLessEqual(d.llm_calls, MAX_LLM_CALLS)
        # every ranked-action number equals its J12 source (nothing re-derived).
        by_key = {(a["targetId"], a["type"]): a for a in DIAG_ACTION_SET["actions"]}
        for a in d.ranked_actions:
            src = by_key[(a.target_id, a.type)]
            self.assertEqual(a.expected_delta, src["expectedDelta"])
            self.assertEqual(a.confidence, src["confidence"])

    def test_degrades_cleanly_when_model_returns_nothing(self) -> None:
        # seam always fails to parse → bounded retry, then deterministic narration.
        d, counter = _run_diagnose(lambda _i: None)
        self.assertLessEqual(counter.get("diagnose"), MAX_LLM_CALLS)
        self.assertEqual(d.llm_calls, MAX_LLM_CALLS)  # 1 call + 1 reparse retry
        self.assertTrue(any("degraded" in w for w in d.warnings))
        # J12 actions are still narrated with verbatim numbers ...
        self.assertEqual(len(d.ranked_actions), 2)
        self.assertEqual(d.ranked_actions[0].expected_delta, 4.2)
        # ... the thin grain is still watchlisted ...
        self.assertIn("adset_new", {w.target_id for w in d.watchlist})
        # ... and no ungrounded test proposals leak through.
        self.assertEqual(d.test_proposals, [])


# ── the tools (offline, faked SaasClient) ─────────────────────────────────────


class _FakeClient:
    """Records calls; serves canned ToolResults by (method, path)."""

    def __init__(self, routes: dict[tuple[str, str], ToolResult]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, Any]] = []

    async def get(self, path: str, headers=None, params=None) -> ToolResult:
        self.calls.append(("GET", path, params))
        return self.routes.get(("GET", path)) or ToolResult(success=False, error=f"no route {path}")

    async def post(self, path: str, headers=None, json=None) -> ToolResult:
        self.calls.append(("POST", path, json))
        return self.routes.get(("POST", path)) or ToolResult(success=False, error=f"no route {path}")


_READ_ROUTES: dict[tuple[str, str], ToolResult] = {
    ("GET", f"{_BASE}/performance"): ToolResult(success=True, data=DIAG_SNAPSHOT),
    ("GET", f"{_BASE}/recommendations"): ToolResult(success=True, data=DIAG_ACTION_SET),
    ("GET", f"{_BASE}/attribute-map"): ToolResult(success=True, data=DIAG_ATTRIBUTE_MAP),
}


class DiagnoseToolTests(unittest.TestCase):
    def _ctx(self) -> dict[str, Any]:
        return {"session_context": {"plan_id": _PLAN_ID}, "headers": {"clientCode": "SYSTEM"}}

    def test_diagnose_tool_offline_reads_and_applies_nothing(self) -> None:
        fake = _FakeClient(dict(_READ_ROUTES))
        counter: dict[str, int] = {}
        singleton = get_diagnose_agent()
        seam = _scripted_llm(DIAG_LLM_DIAGNOSIS, counter)
        with mock.patch("app.agents.adzump2.diagnose.tools._client", lambda: fake), \
             mock.patch.object(singleton, "_llm_json", seam):
            result = asyncio.run(diagnose.execute({"window": "30d"}, self._ctx()))

        self.assertTrue(result.success, result.error)
        data = result.data
        self.assertIn("rankedActions", data)
        self.assertIn("testProposals", data)
        self.assertIn("watchlist", data)
        self.assertIn("signals", data)
        self.assertEqual(data["counts"]["rankedActions"], 2)
        self.assertEqual(data["counts"]["testProposals"], 2)
        self.assertTrue(any(w["targetId"] == "adset_new" for w in data["watchlist"]))
        # the three contract reads happened — and NOTHING mutating (applies nothing).
        methods = {c[0] for c in fake.calls}
        self.assertEqual(methods, {"GET"})
        paths = {c[1] for c in fake.calls}
        self.assertEqual(paths, {k[1] for k in _READ_ROUTES})
        # the read carried the window query param.
        perf_call = next(c for c in fake.calls if c[1].endswith("/performance"))
        self.assertEqual(perf_call[2], {"window": "30d"})

    def test_diagnose_tool_requires_active_plan(self) -> None:
        fake = _FakeClient(dict(_READ_ROUTES))
        with mock.patch("app.agents.adzump2.diagnose.tools._client", lambda: fake):
            result = asyncio.run(diagnose.execute({}, {"session_context": {}}))
        self.assertFalse(result.success)
        self.assertIn("plan", result.error.lower())
        self.assertEqual(fake.calls, [])  # no read attempted without a plan

    def test_diagnose_tool_fails_cleanly_without_snapshot(self) -> None:
        routes = {("GET", f"{_BASE}/performance"): ToolResult(success=False, error="404 no snapshot")}
        fake = _FakeClient(routes)
        with mock.patch("app.agents.adzump2.diagnose.tools._client", lambda: fake):
            result = asyncio.run(diagnose.execute({}, self._ctx()))
        self.assertFalse(result.success)
        self.assertIn("snapshot", result.error.lower())

    def test_propose_action_routes_through_the_gate(self) -> None:
        gated = {
            "type": "REFINE_AUDIENCE", "targetId": "adset_roi",
            "change": {"expand": ["nri_investors"]},
            "significanceVerdict": "SIGNIFICANT", "risk": "LOW", "requiresApproval": True,
        }
        fake = _FakeClient({("POST", f"{_BASE}/actions/propose"): ToolResult(success=True, data=gated)})
        params = {"type": "refine_audience", "target_id": "adset_roi",
                  "change": {"expand": ["nri_investors"]}, "rationale": "scale the winner"}
        with mock.patch("app.agents.adzump2.diagnose.tools._client", lambda: fake):
            result = asyncio.run(propose_action.execute(params, self._ctx()))
        self.assertTrue(result.success, result.error)
        self.assertEqual(len(fake.calls), 1)
        method, path, body = fake.calls[0]
        self.assertEqual((method, path), ("POST", f"{_BASE}/actions/propose"))
        self.assertEqual(body["type"], "REFINE_AUDIENCE")
        self.assertEqual(body["targetId"], "adset_roi")
        self.assertIn("gate", result.summary.lower())

    def test_propose_action_reports_suppression(self) -> None:
        suppressed = {"suppressed": True, "suppressionReason": "below minimum volume"}
        fake = _FakeClient({("POST", f"{_BASE}/actions/propose"): ToolResult(success=True, data=suppressed)})
        params = {"type": "PAUSE_ENTITY", "target_id": "adset_new", "rationale": "kill it"}
        with mock.patch("app.agents.adzump2.diagnose.tools._client", lambda: fake):
            result = asyncio.run(propose_action.execute(params, self._ctx()))
        self.assertTrue(result.success, result.error)
        self.assertIn("SUPPRESSED", result.summary)
        self.assertIn("minimum volume", result.summary)

    def test_propose_action_validates_type(self) -> None:
        fake = _FakeClient({})
        with mock.patch("app.agents.adzump2.diagnose.tools._client", lambda: fake):
            result = asyncio.run(
                propose_action.execute({"type": "NUKE_ACCOUNT", "target_id": "x"}, self._ctx())
            )
        self.assertFalse(result.success)
        self.assertEqual(fake.calls, [])  # never reached the gate


if __name__ == "__main__":
    unittest.main()
