"""Turn decision record (rework slice 1c) - the manual-testing instrument.

Locks the §8 schema facts that matter: repeat_ask fires iff the prescription
re-asks the open rail's field; an untagged rail flags unmatched; prior_capture
rides along so a repeat-ask after a STORED answer is distinguishable from
ordinary repair.
"""
from __future__ import annotations

import asyncio
import json
import unittest

from app.agents.adzump.agent import AdzumpAgent
from app.agents.adzump.observability import log_turn_decision, prescription_field
from tests.agents.adzump._fixtures import SAAS, elicitation, make_session


def _emit(**kw) -> dict:
    defaults = dict(session_id="s1", turn=7, agentic_turn=1, missing=[],
                    steers=[], captures=[], prior_capture=None,
                    open_rail_field=None, open_rail_untagged=False)
    defaults.update(kw)
    with unittest.TestCase().assertLogs("app.agents.adzump.observability", "INFO") as logs:
        log_turn_decision(**defaults)
    return json.loads(logs.output[0].split("turn_decision ", 1)[1])


class PrescriptionFieldTests(unittest.TestCase):
    def test_table(self):
        cases = [
            (["duration - use the present_options tool ..."], "duration"),
            (["competitive analysis - offer it ONCE ...", "duration - ..."],
             "competitive_analysis"),
            (["review & publish - TWO separate steps ..."], None),
            ([], None),
        ]
        for missing, expected in cases:
            with self.subTest(missing=missing[:1]):
                self.assertEqual(prescription_field(missing), expected)


class TurnDecisionRecordTests(unittest.TestCase):
    def test_repeat_ask_flags(self):
        cases = [  # (name, missing-head, rail_field, untagged, repeat, unmatched)
            ("re-asks the open rail's field", "duration - ...", "duration", False,
             True, False),
            ("different field is not a repeat", "budget - ...", "duration", False,
             False, False),
            ("untagged rail is suspicious", "duration - ...", None, True,
             False, True),
            ("review prescribes nothing", "review & publish - ...", "duration", False,
             False, False),
        ]
        for name, head, rail_field, untagged, repeat, unmatched in cases:
            with self.subTest(name):
                record = _emit(missing=[head], open_rail_field=rail_field,
                               open_rail_untagged=untagged)
                self.assertEqual(record["repeat_ask"], repeat)
                self.assertEqual(record["repeat_ask_unmatched"], unmatched)

    def test_record_carries_context(self):
        record = _emit(
            missing=["duration - ...", "budget - ..."],
            steers=["capture_ack"],
            captures=[{"layer": 1, "field": "platform", "value": "Meta",
                       "verdict": "stored"}],
            prior_capture={"field": "platform", "verdict": "stored"},
        )
        self.assertEqual(record["missing"], ["duration", "budget"])
        self.assertEqual(record["prescription"], "duration")
        self.assertEqual(record["captures"][0]["verdict"], "stored")
        self.assertEqual(record["prior_capture"]["field"], "platform")
        self.assertEqual(record["turn"], 7)


class ReminderIntegrationTests(unittest.TestCase):
    """The record actually fires from build_turn_reminder with real capture
    flow: a chip click lands as a layer-1 capture, and prior_capture rotates."""

    def _reminder(self, s, turn=1):
        agent = AdzumpAgent.get_instance()
        with self.assertLogs("app.agents.adzump.observability", "INFO") as logs:
            asyncio.run(agent.build_turn_reminder(s, turn))
        return json.loads(logs.output[0].split("turn_decision ", 1)[1])

    def test_capture_flows_into_record_and_prior_rotates(self):
        s = make_session(
            last_user="30 days", product=SAAS,
            spec={"platform": "Google Ads", "competitive_analysis": "declined"},
            pending_elicitation=elicitation(
                "duration", {"30 days": "30 days", "60 days": "60 days"}),
        )
        record = self._reminder(s)
        self.assertEqual(record["captures"], [
            {"layer": 1, "field": "duration", "value": "30 days",
             "verdict": "stored"}])
        self.assertIn("capture_ack", record["steers"])
        self.assertFalse(record["repeat_ask"])          # duration landed → budget next
        self.assertEqual(record["prescription"], "budget")
        self.assertEqual(s.context["_prior_capture"],
                         {"field": "duration", "verdict": "stored"})
        # Next user message with no capture: the record carries the prior,
        # then rotates it to None.
        s.messages = [{"role": "user", "content": "what does budget mean?"}]
        record2 = self._reminder(s)
        self.assertEqual(record2["prior_capture"],
                         {"field": "duration", "verdict": "stored"})
        self.assertEqual(record2["captures"], [])
        self.assertIsNone(s.context["_prior_capture"])


if __name__ == "__main__":
    unittest.main()
