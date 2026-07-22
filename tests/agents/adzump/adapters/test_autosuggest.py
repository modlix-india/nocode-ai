"""Unit tests for the autosuggest adapter (app/agents/adzump/adapters/autosuggest.py).

The keyword agent answers "why is this keyword here?" from the source+seed that surfaced
it, and that provenance cannot be recovered after the fact — so it must survive the fan-out
merge and dedup.
"""
# regression: the seed used to be dropped twice — inside _one() before the merge ever ran,
# and again by a merge that only kept the keyword string.
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.adapters import autosuggest


class _FakeSource(autosuggest.SuggestionSource):
    def __init__(self, name: str, hits: dict[str, list[str]] | None = None, boom: bool = False):
        self.name = name
        self._hits = hits or {}
        self._boom = boom

    async def fetch(self, seed, *, hl, gl, client):
        if self._boom:
            raise RuntimeError("source down")
        return self._hits.get(seed, [])


def _fetch(seeds, sources, **kw):
    return asyncio.run(
        autosuggest.fetch_suggestions(seeds, sources=sources, client=mock.MagicMock(), **kw)
    )


class ProvenanceTests(unittest.TestCase):
    def test_each_suggestion_carries_its_source_and_seed(self):
        g = _FakeSource("google", {"running shoes": ["running shoes mens"]})
        out = _fetch(["running shoes"], [g])
        self.assertEqual(out, [autosuggest.Suggestion("running shoes mens", "google", "running shoes")])

    def test_seed_survives_when_one_seed_yields_many(self):
        g = _FakeSource("google", {"trail shoes": ["trail shoes waterproof", "trail shoes sale"]})
        self.assertEqual([s.seed for s in _fetch(["trail shoes"], [g])], ["trail shoes"] * 2)

    def test_dedup_keeps_the_first_sighting_as_provenance(self):
        # Same term from two sources — the first to surface it owns the record.
        g = _FakeSource("google", {"shoes": ["shared term"]})
        b = _FakeSource("bing", {"shoes": ["shared term"]})
        out = _fetch(["shoes"], [g, b])
        self.assertEqual(len(out), 1)  # deduped
        self.assertEqual(out[0].source, "google")

    def test_provenance_is_per_seed_not_per_source(self):
        g = _FakeSource("google", {"a shoes": ["x1"], "b shoes": ["x2"]})
        out = {s.keyword: s.seed for s in _fetch(["a shoes", "b shoes"], [g])}
        self.assertEqual(out, {"x1": "a shoes", "x2": "b shoes"})


class FailSoftTests(unittest.TestCase):
    def test_a_failing_source_yields_nothing_and_never_raises(self):
        boom = _FakeSource("brave", boom=True)
        good = _FakeSource("google", {"shoes": ["running shoes"]})
        out = _fetch(["shoes"], [boom, good])
        self.assertEqual([s.keyword for s in out], ["running shoes"])  # the good one still lands
        self.assertEqual(out[0].source, "google")

    def test_no_seeds_or_no_sources_is_empty(self):
        g = _FakeSource("google", {"shoes": ["running shoes"]})
        self.assertEqual(_fetch([], [g]), [])
        self.assertEqual(_fetch(["shoes"], []), [])

    def test_max_per_seed_caps_each_source(self):
        g = _FakeSource("google", {"shoes": [f"shoes {i}" for i in range(20)]})
        self.assertEqual(len(_fetch(["shoes"], [g], max_per_seed=3)), 3)


if __name__ == "__main__":
    unittest.main()
