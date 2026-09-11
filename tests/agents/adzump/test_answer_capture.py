"""Typed-answer validation: field_candidates, _field_traceable,
_set_campaign_spec, and the Custom two-turn path (_capture_tagged_answer).

Merges the old test_answer_parse.py + test_adversarial_probe.py (the G2/G5/M2
live scenarios and the F24 traceability fix) into behavior tables. Bug history
worth keeping: F24 loosened traceability for cue-word corrections ("no wait,
make it 60") and multi-number messages, WITHOUT re-opening invention (model
storing a number the user never typed) or cross-assignment (duration number
stored as budget) - the reject table is that firewall.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump.test_answer_capture -v
"""
from __future__ import annotations

import asyncio
import types
import unittest

from app.agents.adzump.agent import AdzumpAgent
from app.agents.adzump.answer_parse import field_candidates
from app.agents.adzump.tools.campaign_data import (
    _field_traceable, _set_campaign_spec, is_clear_decline_reply,
)

RE = {"product_name": "Sumadhura Solea", "summary": "Luxury 3 & 4 BHK apartments.",
      "business_type": "real estate"}
SC = {"product_data": dict(RE)}


def _set(spec, last_user, params):
    """Run _set_campaign_spec against a minimal session; return (result, spec)."""
    sc = {"campaign_spec": dict(spec), "_spec_set_at": {}, "product_data": dict(RE)}
    session = types.SimpleNamespace(
        messages=[{"role": "user", "content": last_user}], _turn_count=7)
    r = asyncio.run(_set_campaign_spec(params, {"session_context": sc, "_session": session}))
    return r, sc["campaign_spec"]


class FieldCandidatesTests(unittest.TestCase):
    """Extractor locks: budget needs a LOCAL money marker; duration numbers
    never cross into budget and vice versa."""

    def test_table(self):
        multi = "60 day duration, ₹15000/day budget"
        cases = [  # (field, message, must_contain, must_not_contain)
            ("budget", multi, {"₹15,000/day"}, {"₹60/day"}),
            ("duration", multi, {"60 days"}, {"15000 days"}),
            ("duration", "no wait, make it 60", {"60 days"}, None),
            ("duration", "make it 60 days, i have 15 properties", {"60 days", "15 days"}, None),
            ("duration", "30", {"30 days"}, None),
            ("budget", "call me at 5000", set(), None),  # bare number is not money
        ]
        for field, msg, contains, excludes in cases:
            with self.subTest(field=field, msg=msg):
                c = field_candidates(field, msg, "₹")
                self.assertTrue(contains <= c, f"candidates={c}")
                if excludes:
                    self.assertFalse(excludes & c, f"candidates={c}")


class TraceabilityTests(unittest.TestCase):
    """_field_traceable post-F24: accept corrections + volunteered multi-number
    values; still reject invention, cross-assignment, and F1 free-number leaks."""

    def test_accepts(self):
        multi = "60 day duration, ₹15000/day budget"
        for field, value, msg in [
            ("duration", "60 days", "no wait, make it 60"),        # cue-word correction
            ("budget", "₹25,000/day", "actually make it 25k/day"), # normalized correction
            ("duration", "60 days", multi),
            ("budget", "₹15,000/day", multi),
            ("duration", "60 days", "make it 60 days"),
            ("duration", "30 days", "30"),                          # F1 canonical bare number
            ("budget", "₹4,000/day", "4k"),                         # PR2 normalization
            ("competitive_analysis", "declined", "No"),              # chip decline
            ("competitive_analysis", "declined",
             "No, skip competitor analysis for now"),               # F11: comma broke exact-match
        ]:
            with self.subTest(field=field, msg=msg):
                self.assertTrue(_field_traceable(field, value, msg, SC))

    def test_rejects(self):
        multi = "60 day duration, ₹15000/day budget"
        for field, value, msg in [
            ("duration", "45 days", "no wait, make it 60"),   # invention
            ("budget", "₹15/day",
             "make the daily budget ₹500/day, i have 15 properties"),  # global number
            ("budget", "₹60/day", multi),                     # cross-assign duration→budget
            ("duration", "15000 days", multi),                # cross-assign budget→duration
            ("duration", "5 days", "I have 15 properties"),   # F1 free-number leak
            ("budget", "₹5,000/day", "call me at 5000"),      # F1 phone-number leak
            ("duration", "3 days", "make it 3 weeks"),        # wrong unit
            ("budget", "₹4,000/day", "no competitors"),       # off-topic reply
            ("competitive_analysis", "declined",
             "no, change the budget to 20k"),                 # polarity flip
            ("competitive_analysis", "unset", "no"),          # only accepted/declined store
        ]:
            with self.subTest(field=field, value=value, msg=msg):
                self.assertFalse(_field_traceable(field, value, msg, SC))


class SpecCaptureTests(unittest.TestCase):
    """_set_campaign_spec end-to-end: corrections land, volunteered fields land,
    junk inputs never record."""

    def test_corrections_and_volunteered_land(self):
        multi = ("make a google ads campaign for adanithane.com, "
                 "60 day duration, ₹15000/day budget")
        cases = [  # (prior spec, user message, params, field, expected substring)
            ({"duration": "30 days"}, "no wait, make it 60", {"duration": "60 days"},
             "duration", "60"),
            ({"duration": "30 days"}, "no wait, make it 60", {"duration": "60"},
             "duration", "60"),  # bare echo still lands (canonical form is cosmetic)
            ({"duration": "30 days"}, "change duration to 60 days", {"duration": "60 days"},
             "duration", "60 days"),
            ({"budget": "₹10,000/day"}, "actually ₹25,000/day", {"budget": "₹25,000/day"},
             "budget", "₹25,000/day"),
            ({"budget": "₹10,000/day"}, "actually make it 25k/day", {"budget": "₹25,000/day"},
             "budget", "₹25,000/day"),
            ({"platform": "Google Ads"}, "actually switch to Meta", {"platform": "Meta"},
             "platform", "Meta"),
            ({}, multi, {"platform": "Google Ads", "duration": "60 days",
                         "budget": "₹15,000/day"}, "duration", "60 days"),
            ({}, multi, {"platform": "Google Ads", "duration": "60 days",
                         "budget": "₹15,000/day"}, "budget", "₹15,000/day"),
        ]
        for spec, msg, params, field, expected in cases:
            with self.subTest(msg=msg, field=field):
                r, got = _set(spec, msg, params)
                self.assertIn(expected, got.get(field, ""),
                              f"stored={got} summary={r.summary!r} error={r.error!r}")

    def test_junk_inputs_never_record(self):
        for msg, params, field in [
            ("", {"duration": "30 days"}, "duration"),
            ("👍", {"duration": "30 days"}, "duration"),
            ("   ", {"platform": "Google Ads"}, "platform"),
        ]:
            with self.subTest(msg=msg):
                r, got = _set({}, msg, params)
                self.assertNotIn(field, got)

    def test_emoji_is_not_a_decline(self):
        for msg in ("👍", "🤔", "👍 sounds good", "   "):
            with self.subTest(msg=msg):
                self.assertFalse(is_clear_decline_reply(msg))


if __name__ == "__main__":
    unittest.main()
