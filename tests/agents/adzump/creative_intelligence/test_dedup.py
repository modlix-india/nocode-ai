"""Unit: creative_intelligence/dedup.py - the deterministic Tier-1 + Tier-2 tiers.

Locks: exact (md5) and perceptual (pHash) collapse to one higher-signal
representative; hashless creatives pass through; and a genuinely distinct
creative is NEVER dropped (dedup is deterministic, vision never culls).
"""
from __future__ import annotations

import unittest

from app.agents.adzump.creative_intelligence.dedup import dedupe, dedupe_exact, dedupe_perceptual
from app.agents.adzump.creative_intelligence.models import Creative

# 64-bit hex hashes: all-ones and one-bit-off are near-dups; all-ones vs all-zeros are far apart.
ONES = "ffffffffffffffff"
ONES_1BIT = "fffffffffffffffe"
ZEROS = "0000000000000000"


def _c(cid, content="", ph="", active=False, impr=0):
    return Creative(creative_id=cid, content_hash=content, perceptual_hash=ph,
                    is_active=active, metrics={"impressions": impr})


class ExactTierTests(unittest.TestCase):
    def test_same_hash_collapses_keeping_higher_signal(self):
        out = dedupe_exact([_c("a", "H", active=False, impr=10), _c("b", "H", active=True, impr=1)])
        self.assertEqual([c.creative_id for c in out], ["b"])  # active beats more-impressions

    def test_hashless_all_kept(self):
        out = dedupe_exact([_c("a", ""), _c("b", "")])
        self.assertEqual(sorted(c.creative_id for c in out), ["a", "b"])


class PerceptualTierTests(unittest.TestCase):
    def test_near_dup_collapses(self):
        out = dedupe_perceptual([_c("a", ph=ONES, active=True), _c("b", ph=ONES_1BIT)])
        self.assertEqual([c.creative_id for c in out], ["a"])

    def test_distinct_kept(self):
        out = dedupe_perceptual([_c("a", ph=ONES), _c("b", ph=ZEROS)])
        self.assertEqual(sorted(c.creative_id for c in out), ["a", "b"])

    def test_no_phash_passes_through(self):
        out = dedupe_perceptual([_c("a", ph=""), _c("b", ph="")])
        self.assertEqual(sorted(c.creative_id for c in out), ["a", "b"])


class CascadeTests(unittest.TestCase):
    def test_full_cascade_collapses_exact_and_near(self):
        out = dedupe([
            _c("1", "AA", ONES, active=True, impr=10),
            _c("2", "AA", ONES),           # exact dup of 1 (content_hash)
            _c("3", "BB", ONES_1BIT),      # near-dup of 1 (pHash)
            _c("4", "CC", ZEROS, active=True),  # distinct
            _c("5", "", ""),               # hashless -> kept
        ])
        self.assertEqual(sorted(c.creative_id for c in out), ["1", "4", "5"])

    def test_distinct_creative_never_dropped(self):
        out = dedupe([_c("1", "AA", ONES), _c("2", "BB", ZEROS)])
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main()
