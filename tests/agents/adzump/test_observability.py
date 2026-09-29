"""Turn decision record (rework slice 1c) - the manual-testing instrument.

Locks the §8 schema facts that matter: repeat_ask fires iff the prescription
re-asks the open rail's field; an untagged rail flags unmatched; the record
names the prescribed field. The record firing from build_turn_reminder is
tested in test_agent.py (ReminderRecordTests).
"""
from __future__ import annotations

import json
import unittest

from app.agents.adzump.observability import log_turn_decision


def _emit(**kw) -> dict:
    defaults = dict(session_id="s1", turn=7, agentic_turn=1, missing=[],
                    steers=[], captures=[], prior_capture=None,
                    open_rail_field=None, open_rail_untagged=False)
    defaults.update(kw)
    with unittest.TestCase().assertLogs("app.agents.adzump.observability", "INFO") as logs:
        log_turn_decision(**defaults)
    return json.loads(logs.output[0].split("turn_decision ", 1)[1])


class TurnDecisionRecordTests(unittest.TestCase):
    def test_rows(self):
        # (name, missing, rail field, untagged rail, repeat, unmatched, prescription)
        cases = [
            ("re-asks the open rail's field", ["duration - ..."], "duration", False,
             True, False, "duration"),
            ("different field is not a repeat", ["budget - ..."], "duration", False,
             False, False, "budget"),
            ("untagged rail is suspicious", ["duration - ..."], None, True,
             False, True, "duration"),
            ("an offer's head names its field",
             ["competitive analysis - offer it ONCE ...", "duration - ..."], None, False,
             False, False, "competitive_analysis"),
            ("review prescribes nothing", ["review & publish - ..."], "duration", False,
             False, False, None),
            ("nothing missing", [], None, False, False, False, None),
        ]
        for name, missing, rail_field, untagged, repeat, unmatched, field in cases:
            with self.subTest(name):
                record = _emit(missing=missing, open_rail_field=rail_field,
                               open_rail_untagged=untagged)
                self.assertEqual(record["repeat_ask"], repeat)
                self.assertEqual(record["repeat_ask_unmatched"], unmatched)
                self.assertEqual(record["prescription"], field)


if __name__ == "__main__":
    unittest.main()
