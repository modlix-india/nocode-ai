"""Unit: app/agents/adzump/core/dynamic_context.py - the per-turn dynamic context."""
from __future__ import annotations

import unittest

from app.agents.adzump.core.dynamic_context import DynamicContext, _user_said_section
from app.agents.adzump.core.journey import Journey, Step

TOY = DynamicContext(
    journey=Journey("TOY", finish="all done", steps=(
        Step("a", label="A", done=lambda c: "a" in c, prescribe=lambda c: "do a",
             value=lambda c: c.get("a")),
    )),
    reply_rules="## RULES",
    missing_note="NOTE",
)


class RenderTests(unittest.TestCase):
    def test_sections_in_order_and_empty_steers_dropped(self):
        text, progress = TOY.render({}, last_user="hi", agentic_turn=1,
                                    steers=("## NUDGE", "", None))
        headers = [line for line in text.splitlines() if line.startswith("## ")]
        self.assertEqual(headers, ["## NUDGE", "## State", "## User just said",
                                   "## RULES", "## What's still missing (in order - do the top item first)"])
        self.assertIn("NOTE\n1. do a", text)
        self.assertEqual(progress.missing, ("do a",))  # the same walk the text came from


class UserSaidSectionTests(unittest.TestCase):
    def test_later_steps_know_the_message_was_already_acknowledged(self):
        # Every step of one reply sees the same message; a later step read it
        # as new and acknowledged it again (live 2026-09-25: "Meta it is..."
        # before and after the targeting run).
        first, later = _user_said_section("Meta", 1), _user_said_section("Meta", 2)
        self.assertIn("## User just said", first)
        self.assertNotIn("already acknowledged", first)
        self.assertIn("already acknowledged", later)
        for section in (first, later):
            self.assertIn("'''\nMeta\n'''", section)


if __name__ == "__main__":
    unittest.main()
