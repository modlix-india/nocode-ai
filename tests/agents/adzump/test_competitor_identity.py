"""CP-5 v2 identity judge: evidence gathering invariants (aggregator/broker
never become evidence, provenance merge, liveness), batch join enforcement
(row echo, invalid/dead picks re-asked once then null - code never picks
next-best), confidence-to-link mapping, and the judge-failure contract."""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump import competitor_identity
from app.agents.adzump.competitor_identity import (
    EntryEvidence,
    EvidenceUrl,
    _enforce,
    _gather_entry_evidence,
    judge_entry_urls,
)


def _session() -> dict:
    return {"product_data": {"place": {"lat": 12.9, "lng": 77.6,
                                       "address": "Bannerghatta Road"}}}


def _row(entry_id=0, eid="E1", confidence="high", abstain=None) -> dict:
    return {"entry_id": entry_id, "official_url_id": eid,
            "confidence": confidence, "canonical_name": None,
            "abstain_reason": abstain, "reason": "test"}


def _evidence(*urls: EvidenceUrl, name="Nambiar Villas") -> EntryEvidence:
    return EntryEvidence(entry_id=0, name=name, urls=list(urls), notes=[])


_ALIVE = EvidenceUrl(eid="E1", url="https://nambiarprojects.com/bannerghatta",
                     host="nambiarprojects.com", provenance=["gbp_listing"],
                     alive=True, gbp_listing_name="Nambiar Bannerghatta Villas")
_DEAD = EvidenceUrl(eid="E2", url="https://deadsite.com/", host="deadsite.com",
                    provenance=["search_result"], alive=False)


class GatherEvidenceTests(unittest.TestCase):
    """Hard invariants live in gathering: vetted candidates only, exclusions
    surfaced as negative evidence, one Places call, provenance merged."""

    def _gather(self, entry, listings, extracted=None, alive=True,
                summary="Official site of Purva Sparkling Springs"):
        with mock.patch.object(
            competitor_identity, "cached_business_listings",
            new=mock.AsyncMock(return_value=listings),
        ), mock.patch.object(
            competitor_identity, "project_page_from_site",
            new=mock.AsyncMock(return_value=extracted),
        ), mock.patch.object(
            competitor_identity, "is_alive",
            new=mock.AsyncMock(return_value=alive),
        ), mock.patch.object(
            competitor_identity, "_page_summary",
            new=mock.AsyncMock(return_value=summary),
        ):
            return asyncio.run(_gather_entry_evidence(0, entry, _session()))

    def test_aggregator_and_broker_hosts_never_become_evidence(self):
        evidence = self._gather(
            {"name": "Nambiar Villas", "url": "https://99acres.com/nambiar"},
            listings=[{"name": "Nambiar Villas Bannerghatta",
                       "website": "https://nambiarvillasbannerghatta.co.in/"}])
        self.assertEqual(evidence.urls, [])
        self.assertEqual(len(evidence.notes), 2)
        self.assertIn("aggregator", evidence.notes[0])
        self.assertIn("broker-style", evidence.notes[1])

    def test_same_url_merges_provenance_and_listing_name(self):
        evidence = self._gather(
            {"name": "Purva Sparkling Springs",
             "url": "https://purvasparklingspring.com/"},
            listings=[{"name": "Purva Sparkling Springs",
                       "website": "https://purvasparklingspring.com"}],
            extracted="https://purvasparklingspring.com/")
        self.assertEqual(len(evidence.urls), 1)
        self.assertEqual(evidence.urls[0].provenance,
                         ["search_result", "gbp_listing", "page_extraction"])
        self.assertEqual(evidence.urls[0].gbp_listing_name, "Purva Sparkling Springs")
        self.assertTrue(evidence.urls[0].alive)

    def test_alive_candidates_get_page_summaries_dead_do_not(self):
        # Content beats spelling (Kailash 2026-09-08): every alive candidate
        # is content-read so the judge compares what the pages SAY - the only
        # way to tell rainbowmayfair.com from the rainbowmayfairE.com clone.
        alive_evidence = self._gather(
            {"name": "Rainbow Mayfair", "url": "https://rainbowmayfaire.com/"},
            listings=[{"name": "Rainbow Mayfair",
                       "website": "https://rainbowmayfair.com/"}])
        self.assertTrue(all(u.page_summary for u in alive_evidence.urls))
        dead_evidence = self._gather(
            {"name": "Rainbow Mayfair", "url": "https://rainbowmayfaire.com/"},
            listings=[], alive=False)
        self.assertTrue(all(not u.page_summary for u in dead_evidence.urls))

    def test_no_name_guard_word_order_reaches_the_judge(self):
        # The Nambiar failure class: the ladder's name guard dropped word-order
        # variants; the judge must SEE the listing and decide equivalence.
        evidence = self._gather(
            {"name": "Nambiar Villas", "url": None},
            listings=[{"name": "Bannerghatta Villas by Nambiar",
                       "website": "https://nambiarprojects.com/"}])
        self.assertEqual(len(evidence.urls), 1)
        self.assertEqual(evidence.urls[0].gbp_listing_name,
                         "Bannerghatta Villas by Nambiar")


class EnforceTests(unittest.TestCase):
    def _enforce(self, evidence, row, rejudge_row=None):
        with mock.patch.object(
            competitor_identity, "_rejudge_entry",
            new=mock.AsyncMock(return_value=rejudge_row),
        ) as self.rejudge, mock.patch.object(
            competitor_identity, "_fetch_more_evidence",
            new=mock.AsyncMock(return_value=False),
        ):
            return asyncio.run(_enforce(evidence, row, _session()))

    def test_confidence_rows(self):
        rows = [
            ("high links", "high", _ALIVE.url),
            ("medium links", "medium", _ALIVE.url),
            ("low is link-less", "low", None),
        ]
        for label, confidence, expected_url in rows:
            with self.subTest(label):
                judgement = self._enforce(_evidence(_ALIVE),
                                          _row(confidence=confidence))
                self.assertEqual(judgement.url, expected_url)
                self.assertEqual(judgement.status, "judged")

    def test_null_pick_is_link_less(self):
        judgement = self._enforce(_evidence(_ALIVE), _row(eid=None))
        self.assertIsNone(judgement.url)

    def test_invalid_pick_reasks_once_then_null(self):
        rows = [
            ("unknown eid", _row(eid="E9")),
            ("dead pick", _row(eid="E2")),
        ]
        for label, row in rows:
            with self.subTest(label):
                judgement = self._enforce(_evidence(_ALIVE, _DEAD), row,
                                          rejudge_row=None)
                self.assertIsNone(judgement.url)
                self.rejudge.assert_awaited_once()

    def test_thin_evidence_abstain_fetches_once_and_rejudges(self):
        evidence = _evidence(_ALIVE.model_copy())
        with mock.patch.object(
            competitor_identity, "project_page_from_site",
            new=mock.AsyncMock(
                return_value="https://nambiarprojects.com/bannerghatta-villas"),
        ), mock.patch.object(
            competitor_identity, "is_alive",
            new=mock.AsyncMock(return_value=True),
        ), mock.patch.object(
            competitor_identity, "_rejudge_entry",
            new=mock.AsyncMock(return_value=_row(eid="E2")),
        ) as rejudge:
            judgement = asyncio.run(_enforce(
                evidence, _row(eid=None, abstain="thin_evidence"), _session()))
        self.assertEqual([u.eid for u in evidence.urls], ["E1", "E2"])
        self.assertEqual(judgement.url,
                         "https://nambiarprojects.com/bannerghatta-villas")
        rejudge.assert_awaited_once()

    def test_reask_verdict_is_enforced_but_never_reasked_again(self):
        # Code never picks next-best: the re-ask's own answer is validated,
        # and a second bad answer just settles link-less.
        judgement = self._enforce(_evidence(_ALIVE, _DEAD), _row(eid="E9"),
                                  rejudge_row=_row(eid="E1"))
        self.assertEqual(judgement.url, _ALIVE.url)
        judgement = self._enforce(_evidence(_ALIVE, _DEAD), _row(eid="E9"),
                                  rejudge_row=_row(eid="E2"))
        self.assertIsNone(judgement.url)


class JudgeBatchTests(unittest.TestCase):
    def _judge(self, entries, call_results):
        calls = list(call_results)

        async def fake_call(**kwargs):
            self.payloads.append(kwargs["payload"])
            result = calls.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        self.payloads: list = []
        with mock.patch.object(
            competitor_identity, "_gather_entry_evidence",
            new=mock.AsyncMock(side_effect=[
                _evidence(_ALIVE.model_copy(), name=e["name"])
                for e in entries]),
        ), mock.patch(
            "app.services.structured_call.structured_call",
            new=fake_call,
        ):
            return asyncio.run(judge_entry_urls(entries, _session()))

    def test_row_mismatch_reasks_whole_then_fails_link_less(self):
        rows = [
            ("missing row", {"decisions": []}),
            ("wrong echo", {"decisions": [_row(entry_id=7)]}),
            ("duplicate entry_id", {"decisions": [_row(), _row()]}),
        ]
        for label, bad_response in rows:
            with self.subTest(label):
                judgements = self._judge([{"name": "Nambiar Villas"}],
                                         [bad_response, bad_response])
                self.assertEqual(judgements[0].status, "judge_failed")
                self.assertIsNone(judgements[0].url)
                self.assertIn("validation_error", self.payloads[1])

    def test_happy_path_batch(self):
        entries = [{"name": "Nambiar Villas"}]
        judgements = self._judge(entries, [{"decisions": [_row()]}])
        self.assertEqual(judgements[0].url, _ALIVE.url)
        self.assertEqual(judgements[0].picked_eid, "E1")
        self.assertEqual(judgements[0].confidence, "high")

    def test_zero_evidence_entries_skip_the_judge(self):
        with mock.patch.object(
            competitor_identity, "_gather_entry_evidence",
            new=mock.AsyncMock(return_value=EntryEvidence(
                entry_id=0, name="Ghost", urls=[], notes=["excluded x"])),
        ), mock.patch(
            "app.services.structured_call.structured_call",
        ) as call:
            judgements = asyncio.run(
                judge_entry_urls([{"name": "Ghost"}], _session()))
        call.assert_not_called()
        self.assertEqual(judgements[0].status, "no_evidence")
        self.assertIsNone(judgements[0].url)


if __name__ == "__main__":
    unittest.main()
