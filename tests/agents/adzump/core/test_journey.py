"""Unit: app/agents/adzump/core/journey.py - the step engine, on a toy journey."""
from __future__ import annotations

import unittest

from app.agents.adzump.core.journey import Journey, Progress, Status, Step, StepState


def _step(name: str, **kw) -> Step[dict]:
    return Step(name, label=name.upper(), done=lambda c: name in c,
                prescribe=lambda c: f"do {name}", value=lambda c: c.get(name), **kw)


# a -> b (asked on screen while c["asking"] == "b") -> d; c is optional, off by default.
TOY = Journey("TOY", finish="all done", steps=(
    _step("a", fields=("a",)),
    _step("b", requires=("a",), ready=lambda c: c.get("asking") != "b"),
    _step("c", requires=("a",), applies=lambda c: c.get("c_on", False)),
    _step("d", requires=("b",)),
))


class WalkTests(unittest.TestCase):
    def test_statuses_and_prescriptions(self):
        rows = [
            # (case, ctx, statuses a..d, missing, complete)
            ("nothing yet", {}, "open blocked off blocked", ("do a",), False),
            ("next step opens", {"a": 1}, "done open off blocked", ("do b",), False),
            ("an ask on screen waits and blocks completion", {"a": 1, "asking": "b"},
             "done waiting off blocked", (), False),
            ("an optional step in scope is owed", {"a": 1, "b": 1, "d": 1, "c_on": True},
             "done done open done", ("do c",), False),
            ("done beats blocked: a volunteered answer counts", {"b": 1},
             "open done off open", ("do a", "do d"), False),
            ("finish only once every step is off or done", {"a": 1, "b": 1, "d": 1},
             "done done off done", ("all done",), True),
        ]
        for case, ctx, statuses, missing, complete in rows:
            with self.subTest(case):
                progress = TOY.walk(ctx)
                self.assertEqual(" ".join(s.status.value for s in progress.step_states), statuses)
                self.assertEqual((progress.missing, progress.complete), (missing, complete))

    def test_age_is_the_newest_field_write(self):
        progress = TOY.walk({"a": 1}, set_at={"a": 2}, turn=5)
        self.assertEqual(progress.step_states[0].turns_ago, 3)
        self.assertIsNone(progress.step_states[1].turns_ago)

    def test_construction_rejects_a_bad_registry(self):
        rows = [
            ("duplicate name", (_step("a"), _step("a"))),
            ("requires a later step", (_step("a", requires=("b",)), _step("b"))),
            ("requires an unknown step", (_step("a", requires=("typo",)),)),
        ]
        for case, steps in rows:
            with self.subTest(case), self.assertRaises(ValueError):
                Journey("BAD", finish="", steps=steps)


class RenderTests(unittest.TestCase):
    def test_state_line_per_status(self):
        rows = [
            (StepState("A", "x", Status.DONE, None), "- A: x ✓"),
            (StepState("A", "x", Status.DONE, 0), "- A: x ✓ - just set"),
            (StepState("A", "x", Status.DONE, 1), "- A: x ✓ - set 1 turn ago"),
            (StepState("A", "x", Status.DONE, 4), "- A: x ✓ - set 4 turns ago"),
            (StepState("A", None, Status.OPEN, None), "- A: -"),
            (StepState("A", None, Status.WAITING, None), "- A: - (asked - waiting on the reply)"),
            (StepState("A", "x", Status.OFF, None), "- A: x"),
            (StepState("A", None, Status.OFF, None), None),
            (StepState("A", None, Status.BLOCKED, None), None),
        ]
        for state, line in rows:
            with self.subTest(state):
                section = Progress((state,), (), False).state_section()
                self.assertEqual(section, "## State" + (f"\n{line}" if line else ""))

    def test_missing_section(self):
        owed = Progress((), ("do a", "do b"), False).missing_section(note="NOTE")
        self.assertEqual(owed.splitlines()[2:], ["NOTE", "1. do a", "2. do b"])
        waiting = Progress((), (), False).missing_section(note="NOTE")
        self.assertIn("pending on screen", waiting)
        self.assertNotIn("NOTE", waiting)


if __name__ == "__main__":
    unittest.main()
