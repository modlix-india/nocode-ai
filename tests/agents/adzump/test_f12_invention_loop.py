"""F12 · invention-retry-loop on the decline-then-proceed path (below the model).

After a decline, with duration/budget unset, the model invented values (copied
from the prescription's example presets) → traceability guard rejected → it
retried with FRESH values each turn, evading the v5 breaker (which keyed on
exact value-signature). Fix: breaker keys on the rejected FIELD-SET, so varied
invention still trips it; steer says ASK, don't resend. Pure → no model."""
import asyncio
import types
import unittest

from app.agents.adzump.tools.campaign_data import _set_campaign_spec

RE = {"product_name": "Purva Sparkling Springs", "summary": "Premium 3BHK villas."}


def _spec_ctx(spec, last_user):
    sc = {"campaign_spec": dict(spec), "_spec_set_at": {}, "product_data": dict(RE)}
    session = types.SimpleNamespace(
        messages=[{"role": "user", "content": last_user}], _turn_count=12,
    )
    return {"session_context": sc, "_session": session}, sc


class F12InventionLoopTests(unittest.TestCase):
    def test_invented_fields_after_decline_store_nothing_and_steer_ask(self):
        # decline turn — user never gave duration/budget; model invents them
        ctx, sc = _spec_ctx({}, "No, skip competitor analysis for now")
        r = asyncio.run(_set_campaign_spec(
            {"duration": "30 days", "budget": "₹5,000/day"}, ctx))
        self.assertFalse(r.success)
        self.assertNotIn("duration", sc["campaign_spec"])   # nothing invented stored
        self.assertNotIn("budget", sc["campaign_spec"])
        self.assertIn("ask", (r.error or "").lower())       # steer = ASK, not retry

    def test_breaker_fires_on_field_set_despite_differing_values(self):
        # the real hole: fresh invented values each turn used to reset the streak
        ctx, sc = _spec_ctx({}, "no, skip competitors")
        invented = [("30 days", "₹5,000/day"), ("60 days", "₹6,000/day"),
                    ("45 days", "₹8,000/day")]
        last = None
        for dur, bud in invented:
            last = asyncio.run(_set_campaign_spec({"duration": dur, "budget": bud}, ctx))
            self.assertFalse(last.success)
        self.assertIn("STOP", last.error or "")  # fires on 3rd despite different values

    def test_legit_varied_correction_does_not_trip_breaker(self):
        # a TRACEABLE correction stores → resets the streak → never accumulates,
        # so re-keying on field-set can't hard-STOP a real user correction.
        ctx, sc = _spec_ctx({}, "make it 30 days")
        r = asyncio.run(_set_campaign_spec({"duration": "30 days"}, ctx))
        self.assertTrue(r.success)
        self.assertEqual(sc["campaign_spec"]["duration"], "30 days")
        self.assertIsNone(ctx["session_context"].get("_spec_reject_streak"))


if __name__ == "__main__":
    unittest.main()
