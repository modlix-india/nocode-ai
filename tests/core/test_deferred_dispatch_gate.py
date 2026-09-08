"""The deferred-schema gate: dispatch a well-formed first call, bounce a bad one.

The gate used to fire on the first call to any non-hot tool unconditionally,
without ever inspecting the arguments — so a model that had correctly inferred
a simple signature still spent a round-trip being handed a schema it had
evidently already worked out. Replaying real recorded sessions, 76% of
first-calls to tools WITHOUT a full schema in the prompt were already valid.

Now the gate validates first and only bounces on an actual mismatch. These
tests pin both halves of that, plus the structural checks the validation rests
on — because a validator that is too lax dispatches malformed calls, and one
that is too strict silently reintroduces the round-trip it was built to remove.
"""

from __future__ import annotations

import unittest

from app.core.agent import BaseAgent
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult


def _tool(**kw) -> ToolDefinition:
    kw.setdefault("name", "demo")
    kw.setdefault("description", "A demo tool")
    return ToolDefinition(**kw)


P = ToolParameter


class SchemaViolationTests(unittest.TestCase):
    """`_schema_violations` — the structural check the gate rests on."""

    def test_no_declared_parameters_accepts_anything(self) -> None:
        t = _tool(parameters=[])
        self.assertEqual(BaseAgent._schema_violations(t, {"whatever": 1}), [])

    def test_valid_call_passes(self) -> None:
        t = _tool(parameters=[P(name="app_code", type="string", description="d")])
        self.assertEqual(BaseAgent._schema_violations(t, {"app_code": "x"}), [])

    def test_unknown_parameter_is_a_violation(self) -> None:
        t = _tool(parameters=[P(name="name", type="string", description="d")])
        v = BaseAgent._schema_violations(t, {"page_name": "login"})
        self.assertTrue(any("unknown parameter 'page_name'" in x for x in v))

    def test_missing_required_is_a_violation(self) -> None:
        t = _tool(parameters=[
            P(name="repo", type="string", description="d"),
            P(name="pattern", type="string", description="d"),
        ])
        v = BaseAgent._schema_violations(t, {"pattern": "foo"})
        self.assertTrue(any("missing required parameter 'repo'" in x for x in v))

    def test_optional_parameter_may_be_omitted(self) -> None:
        t = _tool(parameters=[
            P(name="app_code", type="string", description="d"),
            P(name="size", type="integer", description="d", required=False, default=25),
        ])
        self.assertEqual(BaseAgent._schema_violations(t, {"app_code": "x"}), [])

    def test_wrong_type_is_a_violation(self) -> None:
        t = _tool(parameters=[P(name="size", type="integer", description="d")])
        v = BaseAgent._schema_violations(t, {"size": "twenty"})
        self.assertTrue(any("expects integer" in x for x in v))

    def test_bool_is_not_accepted_as_a_number(self) -> None:
        """`True` is an `int` in Python; a truthy flag must not pass as a count."""
        t = _tool(parameters=[P(name="size", type="integer", description="d")])
        self.assertTrue(BaseAgent._schema_violations(t, {"size": True}))

    def test_int_is_accepted_as_a_number(self) -> None:
        t = _tool(parameters=[P(name="ratio", type="number", description="d")])
        self.assertEqual(BaseAgent._schema_violations(t, {"ratio": 3}), [])

    def test_enum_membership_is_checked(self) -> None:
        t = _tool(parameters=[
            P(name="mode", type="string", description="d", enum=["a", "b"]),
        ])
        self.assertEqual(BaseAgent._schema_violations(t, {"mode": "a"}), [])
        self.assertTrue(BaseAgent._schema_violations(t, {"mode": "z"}))

    def test_non_dict_input_is_a_violation(self) -> None:
        t = _tool(parameters=[P(name="a", type="string", description="d")])
        self.assertTrue(BaseAgent._schema_violations(t, "not-a-dict"))

    def test_allow_unknown_params_opts_out_of_the_name_check(self) -> None:
        t = _tool(parameters=[P(name="a", type="string", description="d")],
                  allow_unknown_params=True)
        self.assertEqual(BaseAgent._schema_violations(t, {"a": "x", "extra": 1}), [])


class _Agent(BaseAgent):
    async def build_dynamic_context(self, session):  # pragma: no cover
        return ""


def _agent(tools) -> BaseAgent:
    from app.core.context import BaseContext

    return _Agent(name="t", tools=tools, context_builder=BaseContext(),
                  defer_schemas=True)


class DeferredGateTests(unittest.TestCase):
    """The gate itself — dispatch vs bounce, and the fetched_schemas bookkeeping."""

    def test_valid_first_call_dispatches(self) -> None:
        t = _tool(parameters=[P(name="app_code", type="string", description="d")])
        a = _agent([t])
        ctx: dict = {"fetched_schemas": []}
        gate = a._gate_deferred_dispatch("demo", t, ctx, {"app_code": "x"})
        self.assertIsNone(gate, "a well-formed first call must dispatch, not bounce")

    def test_valid_first_call_marks_the_schema_fetched(self) -> None:
        """So later calls skip validation entirely, as a get_tool_schema would."""
        t = _tool(parameters=[P(name="app_code", type="string", description="d")])
        a = _agent([t])
        ctx: dict = {"fetched_schemas": []}
        a._gate_deferred_dispatch("demo", t, ctx, {"app_code": "x"})
        self.assertIn("demo", ctx["fetched_schemas"])

    def test_invalid_first_call_bounces_with_the_schema(self) -> None:
        t = _tool(parameters=[P(name="name", type="string", description="d")])
        a = _agent([t])
        ctx: dict = {"fetched_schemas": []}
        gate = a._gate_deferred_dispatch("demo", t, ctx, {"page_name": "login"})
        self.assertIsInstance(gate, ToolResult)
        self.assertIn("input_schema", gate.summary)

    def test_the_bounce_says_what_was_wrong(self) -> None:
        """A bare schema dump invites a re-guess; the violation names the fix."""
        t = _tool(parameters=[P(name="name", type="string", description="d")])
        a = _agent([t])
        gate = a._gate_deferred_dispatch("demo", t, {"fetched_schemas": []},
                                         {"page_name": "login"})
        self.assertIn("page_name", gate.summary)

    def test_already_fetched_skips_validation_entirely(self) -> None:
        """Post-fetch, _reject_unknown_params owns argument errors, not the gate."""
        t = _tool(parameters=[P(name="name", type="string", description="d")])
        a = _agent([t])
        ctx: dict = {"fetched_schemas": ["demo"]}
        self.assertIsNone(a._gate_deferred_dispatch("demo", t, ctx, {"bogus": 1}))

    def test_meta_tools_are_never_gated(self) -> None:
        t = _tool(name="get_tool_schema",
                  parameters=[P(name="name", type="string", description="d")])
        a = _agent([t])
        self.assertIsNone(
            a._gate_deferred_dispatch("get_tool_schema", t, {"fetched_schemas": []}, {})
        )

    def test_gate_is_inert_when_defer_schemas_is_off(self) -> None:
        from app.core.context import BaseContext

        t = _tool(parameters=[P(name="name", type="string", description="d")])
        a = _Agent(name="t", tools=[t], context_builder=BaseContext(),
                   defer_schemas=False)
        self.assertIsNone(
            a._gate_deferred_dispatch("demo", t, {"fetched_schemas": []}, {"bad": 1})
        )

    def test_legacy_set_shaped_fetched_schemas_still_works(self) -> None:
        """In-flight sessions persisted `fetched_schemas` as a set."""
        t = _tool(parameters=[P(name="app_code", type="string", description="d")])
        a = _agent([t])
        ctx: dict = {"fetched_schemas": {"other"}}
        self.assertIsNone(a._gate_deferred_dispatch("demo", t, ctx, {"app_code": "x"}))
        self.assertIn("demo", ctx["fetched_schemas"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
