"""Unit: creative_intelligence/sweep.py - the Rule-9 repair sweep.

After the sweep, no stored creative points at a broken asset: failures are
removed with a dropped[] diagnostic, totals follow the array, an emptied
record flips to fetchStatus "empty", and --dry-run writes nothing.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.creative_intelligence import store, sweep, verify
from app.agents.adzump.creative_intelligence.models import Competitor, Creative


def _competitor(*creative_ids: str) -> Competitor:
    return Competitor(
        competitor_key="nike.com", name="Nike", fetch_status="ok",
        creatives=[Creative(creative_id=c, file_url=f"/files/{c}.png")
                   for c in creative_ids],
    )


def _run_sweep(competitors, verdicts: dict[str, bool], *, dry_run=False):
    """verdicts: creative_id -> keep?"""
    async def fake_verify(creative):
        return ((True, "") if verdicts.get(creative.creative_id, True)
                else (False, verify.FETCH_FAILED))

    upserts: list[Competitor] = []

    async def fake_upsert(competitor, ctx):
        upserts.append(competitor)
        return "id1"

    with mock.patch.object(store, "list_competitors",
                           new=mock.AsyncMock(return_value=competitors)), \
         mock.patch.object(sweep.store, "upsert_competitor", new=fake_upsert), \
         mock.patch.object(sweep.verify, "verify_creative", new=fake_verify):
        report = asyncio.run(sweep.sweep_library({}, dry_run=dry_run))
    return report, upserts


class SweepTests(unittest.TestCase):
    def test_broken_creatives_removed_with_diagnostics(self):
        comp = _competitor("good", "broken1", "broken2")
        report, upserts = _run_sweep([comp], {"broken1": False, "broken2": False})
        self.assertEqual(report["records_scanned"], 1)
        self.assertEqual(report["creatives_checked"], 3)
        self.assertEqual(report["removed_by_reason"], {verify.FETCH_FAILED: 2})
        self.assertEqual(len(upserts), 1)
        written = upserts[0]
        self.assertEqual([c.creative_id for c in written.creatives], ["good"])
        self.assertEqual(written.total_creatives, 1)
        self.assertEqual({d["creativeId"] for d in written.dropped},
                         {"broken1", "broken2"})
        self.assertEqual(written.fetch_status, "ok")

    def test_emptied_record_flips_to_empty_never_ok(self):
        report, upserts = _run_sweep([_competitor("b")], {"b": False})
        self.assertEqual(upserts[0].fetch_status, "empty")
        self.assertEqual(upserts[0].creatives, [])

    def test_clean_record_is_not_rewritten(self):
        report, upserts = _run_sweep([_competitor("good")], {})
        self.assertEqual(report["records_updated"], 0)
        self.assertEqual(upserts, [])

    def test_dry_run_reports_without_writing(self):
        report, upserts = _run_sweep([_competitor("b")], {"b": False},
                                     dry_run=True)
        self.assertEqual(report["records_updated"], 1)
        self.assertEqual(report["removed_by_reason"], {verify.FETCH_FAILED: 1})
        self.assertEqual(upserts, [])


if __name__ == "__main__":
    unittest.main()
