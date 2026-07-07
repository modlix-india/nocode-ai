"""F13 · deterministic stuck-loop breaker - BaseAgent._stuck_step (below the model).

When every tool call in a turn fails with the SAME tool-name signature N turns
running, the offending tools are quarantined for the rest of the run (so the
model is forced to ask/move on instead of re-calling a tool that keeps
rejecting). Signature = tool NAMES, not inputs (varied invention keeps the same
names). Any success resets it. Pure step → no model, no loop."""
import unittest

from app.core.agent import BaseAgent

N = 4


def _fail(tool):
    return {"tool": tool, "success": False}


def _ok(tool):
    return {"tool": tool, "success": True}


def _noop(tool):  # F15: success but stored nothing new (kept-noop)
    return {"tool": tool, "success": True, "no_progress": True}


def _elicit(tool):  # asked the user - progress
    return {"tool": tool, "success": True, "elicited": True}


def _drive(turns, n_threshold=N):
    """Replay a sequence of turns (each a list of log entries) through the pure
    step; return the list of quarantine-sets emitted per turn."""
    sig, n, out = None, 0, []
    for entries in turns:
        sig, n, to_quar = BaseAgent._stuck_step(entries, sig, n, n_threshold)
        out.append(to_quar)
    return out


class StuckStepTests(unittest.TestCase):
    def test_quarantines_after_N_identical_all_failed(self):
        turns = [[_fail("set_campaign_spec")]] * N
        out = _drive(turns)
        self.assertEqual(out[:N - 1], [set()] * (N - 1))   # no trip before N
        self.assertEqual(out[N - 1], {"set_campaign_spec"})  # trips exactly at N

    def test_signature_is_names_not_values(self):
        # same tool, "different invented values" each turn (values aren't in the
        # signature) → still trips at N. The F12 evasion the breaker must catch.
        turns = [[_fail("set_campaign_spec")] for _ in range(N)]
        self.assertEqual(_drive(turns)[N - 1], {"set_campaign_spec"})

    def test_real_success_resets_streak(self):
        # 3 fails, then a REAL store (success, no no_progress), then fails → no trip
        turns = [[_fail("set_campaign_spec")]] * 3 + [[_ok("set_campaign_spec")]] \
            + [[_fail("set_campaign_spec")]] * 2
        self.assertTrue(all(q == set() for q in _drive(turns)))

    def test_changing_signature_does_not_accumulate(self):
        # alternating different failed tools → never N-in-a-row of one sig
        turns = [[_fail("a")], [_fail("b")], [_fail("a")], [_fail("b")], [_fail("a")]]
        self.assertTrue(all(q == set() for q in _drive(turns)))

    def test_partial_success_in_turn_is_not_stuck(self):
        # a turn with one failed + one ok tool is NOT all-failed → no signature
        turns = [[_fail("x"), _ok("y")]] * (N + 2)
        self.assertTrue(all(q == set() for q in _drive(turns)))

    def test_empty_turn_no_trip(self):
        self.assertTrue(all(q == set() for q in _drive([[]] * (N + 1))))

    def test_resets_after_trip_so_a_second_tool_can_retrip(self):
        turns = [[_fail("a")]] * N + [[_fail("b")]] * N
        out = _drive(turns)
        self.assertEqual(out[N - 1], {"a"})
        self.assertEqual(out[2 * N - 1], {"b"})


class F15NoProgressTests(unittest.TestCase):
    def test_kept_noop_turns_trip_at_N(self):  # the F15 regression
        turns = [[_noop("set_campaign_spec")]] * N
        out = _drive(turns)
        self.assertEqual(out[:N - 1], [set()] * (N - 1))
        self.assertEqual(out[N - 1], {"set_campaign_spec"})  # quarantines the noop tool

    def test_real_store_midstreak_resets(self):
        turns = [[_noop("set_campaign_spec")]] * 3 + [[_ok("set_campaign_spec")]] \
            + [[_noop("set_campaign_spec")]] * 3
        self.assertTrue(all(q == set() for q in _drive(turns)))

    def test_elicitation_resets(self):  # asking the user is progress
        turns = [[_noop("set_campaign_spec")]] * 3 + [[_elicit("present_options")]] \
            + [[_noop("set_campaign_spec")]] * 3
        self.assertTrue(all(q == set() for q in _drive(turns)))

    def test_mixed_failed_and_noop_same_sig_trips(self):
        # model alternating reject/kept-noop on the same tool must not dodge it
        turns = [[_fail("set_campaign_spec")], [_noop("set_campaign_spec")]] * N
        self.assertIn("set_campaign_spec", _drive(turns)[-1] | _drive(turns)[-2])

    def test_noop_bundled_with_real_store_is_not_stuck(self):
        turns = [[_noop("a"), _ok("b")]] * (N + 2)
        self.assertTrue(all(q == set() for q in _drive(turns)))

    def test_bare_success_never_stuck(self):
        self.assertTrue(all(q == set() for q in _drive([[_ok("x")]] * (N + 1))))


if __name__ == "__main__":
    unittest.main()
