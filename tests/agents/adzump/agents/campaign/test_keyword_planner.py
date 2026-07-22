"""Unit tests for the Keyword Planner circuit breaker
(app/agents/adzump/adapters/google/keyword_planner.py): it opens after N
consecutive failures or a single definitive back-off signal (429), fails fast
while open, resets on success, and closes again once the cooldown elapses.
"""
# regression: a rate-limit (429) must open the breaker immediately so the caller
# stops hammering the Planner, and success must reset the consecutive-failure count.
from __future__ import annotations

import unittest
from unittest import mock

from app.agents.adzump.adapters.google import keyword_planner as kp


class BreakerTests(unittest.TestCase):
    def setUp(self):
        # Per-process module state — reset before each test.
        kp._breaker_failures = 0
        kp._breaker_open_until = 0.0

    def test_fresh_breaker_is_closed(self):
        kp._breaker_check()  # does not raise

    def test_opens_only_after_threshold_consecutive_failures(self):
        for _ in range(kp._BREAKER_THRESHOLD - 1):
            kp._breaker_record(ok=False)
        kp._breaker_check()  # threshold-1 failures: still closed

        kp._breaker_record(ok=False)  # the Nth failure opens it
        with self.assertRaises(kp.PlannerUnavailable):
            kp._breaker_check()

    def test_success_resets_the_failure_count(self):
        for _ in range(kp._BREAKER_THRESHOLD - 1):
            kp._breaker_record(ok=False)
        kp._breaker_record(ok=True)  # reset
        self.assertEqual(kp._breaker_failures, 0)
        # A fresh run of failures must again take the full threshold to open.
        for _ in range(kp._BREAKER_THRESHOLD - 1):
            kp._breaker_record(ok=False)
        kp._breaker_check()  # still closed

    def test_trip_opens_immediately(self):
        with mock.patch.object(kp.time, "monotonic", return_value=1000.0):
            kp._breaker_trip("429 rate limit")
            with self.assertRaises(kp.PlannerUnavailable):
                kp._breaker_check()

    def test_closes_after_cooldown_elapses(self):
        clock = mock.Mock(return_value=1000.0)
        with mock.patch.object(kp.time, "monotonic", clock):
            kp._breaker_trip("429 rate limit")
            with self.assertRaises(kp.PlannerUnavailable):
                kp._breaker_check()  # still within the cooldown window
            clock.return_value = 1000.0 + kp._BREAKER_COOLDOWN_SECONDS + 1
            kp._breaker_check()  # cooldown elapsed -> closed again


if __name__ == "__main__":
    unittest.main()
