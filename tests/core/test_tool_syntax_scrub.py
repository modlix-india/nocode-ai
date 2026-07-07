"""F16 · scrub leaked tool-call syntax from assistant text — BaseAgent._strip_tool_syntax.

The orchestrator sometimes echoes the internal prescription (e.g.
`present_options(question=..., field="duration")`) into the user bubble. The scrub
removes standalone leaked tool-call lines, keyed off the live tool registry, while
leaving legit prose and non-tool code untouched. Pure → no model."""
import unittest

from app.core.agent import BaseAgent

TOOLS = {
    "present_options", "set_campaign_spec", "confirm_location",
    "analyze_competitors", "launch_campaign", "fetch_meta_parent_accounts",
}


def scrub(text):
    return BaseAgent._strip_tool_syntax(text, TOOLS)


class StripToolSyntaxTests(unittest.TestCase):
    def test_strips_the_live_f16_leak(self):
        text = ('Great choice! Now, let\'s set the duration for your campaign.\n\n'
                'present_options(question="How long should the campaign run?", '
                'options=[{"label":"30 days","value":"30 days"}], field="duration")')
        cleaned, n = scrub(text)
        self.assertEqual(n, 1)
        self.assertNotIn("present_options(", cleaned)
        self.assertEqual(cleaned, "Great choice! Now, let's set the duration for your campaign.")

    def test_strips_empty_call_form(self):
        cleaned, n = scrub("Let me pull your accounts.\nfetch_meta_parent_accounts()")
        self.assertEqual(n, 1)
        self.assertNotIn("fetch_meta_parent_accounts(", cleaned)

    def test_strips_set_campaign_spec_leak(self):
        cleaned, n = scrub('Got it.\nset_campaign_spec(platform="Google Ads")')
        self.assertEqual(n, 1)
        self.assertEqual(cleaned, "Got it.")

    # ── false-positive guards: legit prose / code must survive untouched ──
    def test_prose_mentioning_tool_survives(self):
        text = "I'll use present_options to ask you, then confirm_location next."
        cleaned, n = scrub(text)
        self.assertEqual(n, 0)
        self.assertEqual(cleaned, text)

    def test_non_tool_code_survives(self):
        text = "Run df.head() and range(10) to check."  # not registered tools
        self.assertEqual(scrub(text), (text, 0))

    def test_tool_name_without_paren_survives(self):
        text = "The present_options step is next."
        self.assertEqual(scrub(text), (text, 0))

    def test_no_tools_is_noop(self):
        text = 'present_options(field="x")'
        self.assertEqual(BaseAgent._strip_tool_syntax(text, set()), (text, 0))

    def test_empty_text(self):
        self.assertEqual(scrub(""), ("", 0))


if __name__ == "__main__":
    unittest.main()
