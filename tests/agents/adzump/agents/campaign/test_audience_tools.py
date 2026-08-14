"""Unit tests for the audience agent's tools and phase machine
(app/agents/adzump/agents/campaign/google/audience/{tools,context}.py).
"""

# regression: a non-targetable PARENT must still resolve its children's ancestry — indexing
# only the targetable entries silently truncates the path the agent reads to tell "people
# buying this" from "people who work in this" apart; and a finished phase must stop being
# re-injected, or the model answers it twice and the second answer is empty.
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.adapters.google import audience_taxonomy as taxonomy
from app.agents.adzump.agents.campaign.google.audience import tools
from app.agents.adzump.agents.campaign.google.audience.context import (
    Phase,
    current_phase,
)

_INDIA_ONLY = [
    {"channel": {"availabilityMode": "ALL_CHANNELS"}, "locale": [{"countryCode": "IN"}]}
]
_NOWHERE = [
    {"channel": {"availabilityMode": "ALL_CHANNELS"}, "locale": [{"countryCode": "GB"}]}
]


def _entry(
    entry_id,
    name,
    parent="",
    availabilities=None,
    resource=taxonomy.DETAILED_DEMOGRAPHIC,
):
    return taxonomy.TaxonomyEntry(
        resource=resource,
        resource_name=f"customers/1/detailedDemographics/{entry_id}",
        entry_id=entry_id,
        name=name,
        parent=parent,
        availabilities=availabilities if availabilities is not None else _INDIA_ONLY,
    )


class FetchTests(unittest.TestCase):
    def _run(self, rows):
        state = {
            "aud_customer_id": "1",
            "aud_channel_type": "DEMAND_GEN",
            "aud_country": "IN",
        }

        async def fake_fetch(resource, **kw):
            return rows if resource == taxonomy.DETAILED_DEMOGRAPHIC else []

        with mock.patch.object(taxonomy, "fetch", new=fake_fetch):
            res = asyncio.run(
                tools._fetch_audience_segments({}, {"session_context": state})
            )
        return res, state

    def test_ancestry_resolves_through_a_non_targetable_parent(self):
        parent = _entry(10, "Employment", availabilities=_NOWHERE)
        child = _entry(11, "Industry", parent=parent.resource_name)
        leaf = _entry(12, "Construction Industry", parent=child.resource_name)
        res, state = self._run([parent, child, leaf])

        self.assertTrue(res.success)
        by_id = {c["id"]: c for c in state["aud_candidates"]}
        # the unavailable parent is not offered...
        self.assertNotIn(10, by_id)
        # ...but it still names the branch its children sit on
        self.assertEqual(
            by_id[12]["path"], ["Employment", "Industry", "Construction Industry"]
        )

    def test_only_targetable_entries_become_candidates(self):
        res, state = self._run(
            [_entry(20, "Reachable"), _entry(21, "Elsewhere", availabilities=_NOWHERE)]
        )
        self.assertTrue(res.success)
        self.assertEqual([c["label"] for c in state["aud_candidates"]], ["Reachable"])

    def test_nothing_targetable_is_an_error_not_an_empty_set(self):
        res, state = self._run([_entry(30, "Elsewhere", availabilities=_NOWHERE)])
        self.assertFalse(res.success)
        self.assertNotIn("aud_candidates", state)


class SubmitSegmentsTests(unittest.TestCase):
    """The one gate that matters: an invented id reaches the wrong people, or nobody, and
    nothing downstream would report it."""

    def _state(self):
        return {
            "aud_candidates": [
                {
                    "id": 80071,
                    "ref": "customers/1/userInterests/80071",
                    "label": "Apartments",
                    "kind": "IN_MARKET",
                    "path": ["Real Estate", "Apartments"],
                }
            ]
        }

    def _submit(self, state, segments):
        return asyncio.run(
            tools._submit_segments({"segments": segments}, {"session_context": state})
        )

    def test_an_invented_ref_is_rejected(self):
        state = self._state()
        res = self._submit(state, [{"ref": "customers/1/userInterests/99999"}])
        self.assertFalse(res.success)
        self.assertNotIn("aud_segments", state)

    def test_an_id_and_a_resource_name_reach_the_same_candidate(self):
        for key in ("80071", "customers/1/userInterests/80071"):
            state = self._state()
            res = self._submit(state, [{"ref": key, "rationale": "r"}])
            self.assertTrue(res.success, key)
            self.assertEqual(
                state["aud_segments"][0]["ref"], "customers/1/userInterests/80071"
            )

    def test_submitting_replaces_rather_than_appends(self):
        state = self._state()
        self._submit(state, [{"ref": "80071"}])
        self._submit(state, [{"ref": "80071"}])
        self.assertEqual(len(state["aud_segments"]), 1)


class PhaseTests(unittest.TestCase):
    def test_a_run_starts_at_select(self):
        self.assertIs(current_phase({}), Phase.SELECT)

    def test_segments_move_it_to_demographics(self):
        self.assertIs(
            current_phase({"aud_segments": [{"ref": "x"}]}), Phase.DEMOGRAPHICS
        )

    def test_answered_demographics_end_the_run(self):
        # "no narrowing" is a real answer — it dumps to a spec of empty lists, and treating
        # that as unanswered re-asks the question and overwrites the answer with nothing.
        state = {"aud_segments": [{"ref": "x"}], "aud_demographics": {"genders": []}}
        self.assertIsNone(current_phase(state))


if __name__ == "__main__":
    unittest.main()
