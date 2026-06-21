"""F26 — decline→reverse must not leave a stale competitive_analysis_declined flag.

Sequence: user declines competitor analysis ("no thanks" → flag="true"), then
reverses ("actually yes, analyze competitors") → analyze_competitors runs (or
competitors are added by name). The flag was never cleared → the session held
BOTH competitor_names=[...] AND declined="true" (contradictory) → the review
summary / persisted launch record could report 'declined' despite analysis.

Fix (panel: Boris/Lance/Kiran):
  B1 — clear_competitor_decline (campaign_data) pops the flag + provenance, called
       at the competitor-store sites (fresh analysis unconditional; lookup-add
       guarded; NOT on pure removal).
  B2 — business_storage._build_full_record backstops the DURABLE record so
       attempted+declined can never both persist (covers un-instrumented paths).

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump.test_f26_decline_cleared -v
"""
from __future__ import annotations

import unittest

from app.agents.adzump.tools.campaign_data import clear_competitor_decline
from app.agents.adzump.services.business_storage import _build_full_record


# ── B1 · the clear helper (pop flag + set_at, idempotent, surgical) ───────
class ClearHelperTests(unittest.TestCase):
    def test_pops_flag_and_provenance(self):
        sc = {"campaign_spec": {"platform": "Google Ads",
                                "competitive_analysis_declined": "true"},
              "_spec_set_at": {"competitive_analysis_declined": 3, "platform": 1}}
        self.assertTrue(clear_competitor_decline(sc))
        self.assertNotIn("competitive_analysis_declined", sc["campaign_spec"])
        self.assertNotIn("competitive_analysis_declined", sc["_spec_set_at"])
        self.assertIn("platform", sc["campaign_spec"])            # untouched
        self.assertIn("platform", sc["_spec_set_at"])

    def test_idempotent_noop_when_absent(self):
        sc = {"campaign_spec": {"platform": "Google Ads"}, "_spec_set_at": {}}
        self.assertFalse(clear_competitor_decline(sc))
        self.assertEqual(sc["campaign_spec"], {"platform": "Google Ads"})

    def test_missing_dicts_safe(self):
        self.assertFalse(clear_competitor_decline({}))            # no crash


# ── B2 · the durable launch record stays honest ──────────────────────────
RE = {"business_type": "real estate", "product_name": "Sattva Bliss", "summary": "x"}


def _rec(spec, *, competitors=None):
    sc = {"product_data": dict(RE), "campaign_spec": dict(spec)}
    if competitors is not None:
        sc["competitor_analysis"] = {"competitors": competitors}
    return _build_full_record(sc, "https://example.com")["campaign"]["competitive"]


class LaunchRecordTests(unittest.TestCase):
    def test_contradiction_persists_as_analyzed_not_declined(self):
        # decline→reverse end state: analysis ran (names) AND stale flag present.
        c = _rec({"platform": "Google Ads", "competitive_analysis_declined": "true"},
                 competitors=[{"name": "Prestige"}, {"name": "Brigade"}])
        self.assertTrue(c["attempted"])
        self.assertFalse(c["declined"], "must not persist declined alongside attempted")

    def test_zero_result_analysis_not_declined(self):
        # reversed + analysis ran but found nothing → attempted, not declined.
        c = _rec({"platform": "Google Ads", "competitive_analysis_declined": "true"},
                 competitors=[])
        self.assertTrue(c["attempted"])
        self.assertFalse(c["declined"])

    def test_genuine_decline_still_persists_declined(self):
        # never analyzed + declined → the real decline must still record.
        c = _rec({"platform": "Google Ads", "competitive_analysis_declined": "true"})
        self.assertFalse(c["attempted"])
        self.assertTrue(c["declined"])

    def test_neither_attempted_nor_declined(self):
        c = _rec({"platform": "Google Ads"})
        self.assertFalse(c["attempted"])
        self.assertFalse(c["declined"])


# ── end-to-end consistency: after clear, no contradiction reaches the record ──
class PostClearConsistencyTests(unittest.TestCase):
    def test_clear_then_record_is_consistent(self):
        # simulate the tool path: contradiction state → clear → build record.
        sc = {"product_data": dict(RE),
              "campaign_spec": {"platform": "Google Ads",
                                "competitive_analysis_declined": "true"},
              "_spec_set_at": {"competitive_analysis_declined": 4},
              "competitor_analysis": {"competitors": [{"name": "Prestige"}]}}
        self.assertTrue(clear_competitor_decline(sc))
        c = _build_full_record(sc, "https://example.com")["campaign"]["competitive"]
        self.assertTrue(c["attempted"])
        self.assertFalse(c["declined"])
        self.assertNotIn("competitive_analysis_declined", sc["campaign_spec"])


if __name__ == "__main__":
    unittest.main()
