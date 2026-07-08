"""Unit: app/agents/adzump2 — agent construction, tool registry, completeness
rail, and the update_plan → plan_completeness stash contract.

No network, no LLM, no DB. The SaasClient is patched at the CLASS level
(``SaasClient._request``) so it intercepts every instance regardless of how
the tools module holds its client (module singleton, lazy factory, per-call).

Covers:
  (a) Adzump2Agent constructs via get_instance() (singleton, name="adzump2")
  (b) ALL_TOOLS: the 5 plan tools + present_options + web_fetch, unique names,
      description + parameters on every definition
  (c) build_turn_reminder: no plan → "create a plan"; incomplete → names the
      missing slot (budget); complete → steers to validate
  (d) update_plan / get_completeness stash plan_completeness into
      context["session_context"]

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump2.test_agent -v
"""
from __future__ import annotations

import asyncio
import types
import unittest
from typing import Any
from unittest import mock

from app.config import settings

# Defensive: provider-key checks must never bite an offline unit test.
# Set BEFORE importing the agent package (harmless if keys are configured).
for _key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "MINIMAX_API_KEY"):
    if not getattr(settings, _key, ""):
        setattr(settings, _key, "offline-test-key")

from app.core.tools.base import ToolDefinition, ToolResult
from app.core.tools.http_client import SaasClient
from app.agents.adzump2.agent import Adzump2Agent
from app.agents.adzump2.tools.registry import ALL_TOOLS

from tests.agents.adzump2.fixtures import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_MISSING_BUDGET_CREATIVES,
    OBJECTIVE_PATCH,
    fake_plan,
)

PLAN_TOOL_NAMES = {
    "create_plan", "get_plan", "update_plan", "get_completeness", "validate_plan",
}

AUTH_HEADERS = {
    "Authorization": "Bearer offline-test",
    "clientCode": "SYSTEM",
    "appCode": "adzump",
}


def _tool(name: str) -> ToolDefinition:
    """Fetch a tool from the registry by name (fails loudly if missing)."""
    by_name = {t.name: t for t in ALL_TOOLS}
    if name not in by_name:
        raise AssertionError(f"tool {name!r} not in ALL_TOOLS: {sorted(by_name)}")
    return by_name[name]


def _fill_params(tool: ToolDefinition, *, plan_id: str | None = None,
                 patch: dict | None = None, seed: dict | None = None) -> dict[str, Any]:
    """Adapt scripted intent to the tool's declared parameter names.

    The adzump2 package is authored in parallel, so exact parameter names are
    its choice — this maps by shape: the plan id goes to a string param whose
    name mentions plan/id; the merge patch (or create seed) goes to the first
    object param; name/productId seed values go to matching string params.
    """
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


def _fake_session(context: dict | None = None) -> types.SimpleNamespace:
    """Minimal BaseSession stand-in for build_turn_reminder."""
    s = types.SimpleNamespace()
    s.context = context if context is not None else {}
    s.messages = [{"role": "user", "content": "set up a campaign for my project"}]
    s._turn_count = 1
    s.session_id = "SYSTEM_test0001"
    s.auth = None
    return s


def _reminder(agent: Adzump2Agent, context: dict) -> str:
    return asyncio.run(agent.build_turn_reminder(_fake_session(context), 1))


# ── (a) construction ───────────────────────────────────────────────────────
class AgentConstructionTests(unittest.TestCase):
    def test_get_instance_constructs_singleton(self):
        agent = Adzump2Agent.get_instance()
        self.assertIsInstance(agent, Adzump2Agent)
        self.assertIs(Adzump2Agent.get_instance(), agent)

    def test_agent_name_is_adzump2(self):
        self.assertEqual(Adzump2Agent.get_instance().name, "adzump2")

    def test_agent_registers_all_tools(self):
        agent = Adzump2Agent.get_instance()
        for name in PLAN_TOOL_NAMES:
            self.assertIn(name, agent.tools, f"agent.tools missing {name}")


# ── (b) registry ───────────────────────────────────────────────────────────
class RegistryTests(unittest.TestCase):
    def test_contains_plan_tools_and_helpers(self):
        names = {t.name for t in ALL_TOOLS}
        for expected in PLAN_TOOL_NAMES | {"present_options", "web_fetch"}:
            self.assertIn(expected, names)

    def test_names_unique(self):
        names = [t.name for t in ALL_TOOLS]
        self.assertEqual(len(names), len(set(names)), f"duplicate tool names: {names}")

    def test_every_tool_has_description_and_parameters(self):
        for t in ALL_TOOLS:
            self.assertIsInstance(t, ToolDefinition)
            self.assertTrue(t.description and t.description.strip(),
                            f"{t.name}: empty description")
            self.assertIsInstance(t.parameters, list, f"{t.name}: parameters not a list")

    def test_plan_tools_are_executable_with_typed_params(self):
        for name in PLAN_TOOL_NAMES:
            t = _tool(name)
            self.assertIsNotNone(t.execute, f"{name}: no execute function")
            self.assertTrue(asyncio.iscoroutinefunction(t.execute),
                            f"{name}: execute must be async")
            for p in t.parameters:
                self.assertTrue(p.name and p.type and p.description,
                                f"{name}.{p.name}: incomplete ToolParameter")

    def test_update_plan_takes_an_object_patch(self):
        t = _tool("update_plan")
        self.assertTrue(any(p.type == "object" for p in t.parameters),
                        "update_plan needs an object param for the RFC-7386 merge patch")


# ── (c) completeness rail (build_turn_reminder) ────────────────────────────
class TurnReminderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent = Adzump2Agent.get_instance()

    def test_no_plan_steers_to_create(self):
        text = _reminder(self.agent, {})
        self.assertTrue(text, "reminder must not be empty with no plan")
        low = text.lower()
        self.assertIn("create", low, f"no-plan rail should steer to create a plan: {text!r}")
        self.assertIn("plan", low)

    def test_incomplete_names_missing_slot(self):
        ctx = {
            "plan_id": "cp_01HTEST0001",
            "plan_completeness": dict(COMPLETENESS_MISSING_BUDGET_CREATIVES),
        }
        text = _reminder(self.agent, ctx)
        self.assertIn("budget", text.lower(),
                      f"rail must name the missing 'budget' slot: {text!r}")

    def test_complete_steers_to_validate(self):
        ctx = {
            "plan_id": "cp_01HTEST0001",
            "plan_completeness": dict(COMPLETENESS_COMPLETE),
        }
        text = _reminder(self.agent, ctx)
        self.assertIn("validate", text.lower(),
                      f"complete rail should steer to validate: {text!r}")

    def test_reminder_is_pure_read(self):
        ctx = {
            "plan_id": "cp_01HTEST0001",
            "plan_completeness": dict(COMPLETENESS_MISSING_BUDGET_CREATIVES),
        }
        before = {k: v for k, v in ctx.items()}
        _reminder(self.agent, ctx)
        self.assertEqual(ctx["plan_completeness"], before["plan_completeness"],
                         "build_turn_reminder must not mutate plan_completeness")


# ── (d) update_plan / get_completeness stash the completeness ──────────────
class FakeSaasRequests:
    """Class-level SaasClient._request replacement returning canned ToolResults.

    Both completeness sources return the SAME payload (embedded in the PATCH
    response AND served by GET .../completeness) so the stash assertion holds
    whichever way the tool derives it.

    NOTE: patched as a class attribute, a callable OBJECT is not a descriptor,
    so ``client._request(method, path, ...)`` calls ``__call__`` WITHOUT the
    client instance — the signature below starts at ``method`` on purpose.
    """

    def __init__(self, completeness: dict[str, Any]) -> None:
        self.completeness = completeness
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, method: str, path: str,
                       headers: dict | None = None, json: Any = None,
                       params: dict | None = None) -> ToolResult:
        self.calls.append((method, path))
        if path.rstrip("/").endswith("/completeness"):
            return ToolResult(success=True, data=dict(self.completeness))
        plan = fake_plan()
        plan["completeness"] = dict(self.completeness)
        if method == "PATCH":
            plan["revision"] = plan["revision"] + 1
        return ToolResult(success=True, data=plan)


class UpdatePlanStashTests(unittest.TestCase):
    def _run(self, tool_name: str, **fill: Any) -> tuple[ToolResult, dict, FakeSaasRequests]:
        tool = _tool(tool_name)
        fake = FakeSaasRequests(COMPLETENESS_MISSING_BUDGET_CREATIVES)
        session_context: dict[str, Any] = {"plan_id": fake_plan()["id"]}
        context: dict[str, Any] = {
            "session_id": "SYSTEM_test0001",
            "headers": dict(AUTH_HEADERS),
            "client_code": "SYSTEM",
            "app_code": "adzump",
            "session_context": session_context,
        }
        params = _fill_params(tool, plan_id=session_context["plan_id"], **fill)
        with mock.patch.object(SaasClient, "_request", fake):
            result = asyncio.run(tool.execute(params, context))
        return result, session_context, fake

    def test_update_plan_stashes_completeness(self):
        result, sc, fake = self._run("update_plan", patch=dict(OBJECTIVE_PATCH))
        self.assertTrue(result.success,
                        f"patch should succeed; error={result.error!r} calls={fake.calls}")
        self.assertIn("plan_completeness", sc,
                      "update_plan must stash plan_completeness into session_context")
        stashed = sc["plan_completeness"]
        self.assertFalse(stashed.get("complete"))
        self.assertIn("budget", stashed.get("missingRequired", []))

    def test_get_completeness_stashes_completeness(self):
        result, sc, fake = self._run("get_completeness")
        self.assertTrue(result.success,
                        f"completeness read should succeed; error={result.error!r} calls={fake.calls}")
        self.assertIn("plan_completeness", sc)
        self.assertIn("creatives", sc["plan_completeness"].get("missingRequired", []))

    def test_failed_patch_returns_clean_error(self):
        tool = _tool("update_plan")

        async def _fail(_client, method, path, headers=None, json=None, params=None):
            return ToolResult(success=False, error="HTTP 404: Plan not found")

        context: dict[str, Any] = {
            "headers": dict(AUTH_HEADERS),
            "session_context": {"plan_id": "cp_missing"},
        }
        params = _fill_params(tool, plan_id="cp_missing", patch=dict(OBJECTIVE_PATCH))
        with mock.patch.object(SaasClient, "_request", _fail):
            result = asyncio.run(tool.execute(params, context))
        self.assertFalse(result.success)
        self.assertTrue(result.error, "failed patch must carry an error message")


if __name__ == "__main__":
    unittest.main()
