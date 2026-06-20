"""PR1a · project-identity grounding (below the model).

- `_build_brief` now carries the user's `note` (their ownership claim) + an
  explicit "claimed to be for THIS product" anchor.
- `_saved_summary` drops the redundant "(role)" when name==role (PR4) and adds a
  non-blocking hedge on brand-defining assets (hero/logo) the model can't verify.
Pure functions → no agent, no live model."""
import unittest

from app.agents.adzump.tools.asset_manage import _build_brief, _saved_summary


def _sctx(name="Purva Sparkling Springs", summary="Premium 3BHK villas."):
    return {"product_data": {"product_name": name, "summary": summary}}


class BuildBriefTests(unittest.TestCase):
    def test_carries_user_note(self):
        out = _build_brief(_sctx(), note="this is our logo")
        self.assertIn('The user said about these image(s): "this is our logo"', out)

    def test_no_note_line_when_absent(self):
        self.assertNotIn("The user said about", _build_brief(_sctx(), note=""))
        self.assertNotIn("The user said about", _build_brief(_sctx()))

    def test_project_anchor_and_name(self):
        out = _build_brief(_sctx(name="Sumadhura Epitome"))
        self.assertIn("THIS product", out)
        self.assertIn("Sumadhura Epitome", out)

    def test_note_trimmed_and_capped(self):
        out = _build_brief(_sctx(), note="  x" * 400)
        line = [l for l in out.splitlines() if l.startswith("The user said")][0]
        self.assertLessEqual(len(line), 340)  # 300-char cap + wrapper


class SavedSummaryTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(_saved_summary([]), [])

    def test_dedups_name_equals_role(self):  # PR4: "Saved your logo." not "logo (logo)"
        out = _saved_summary([{"role": "logo", "name": "logo"}])
        self.assertEqual(out[0], "Saved your logo.")

    def test_keeps_distinct_name(self):
        out = _saved_summary([{"role": "floor_plan", "name": "3bhk-plan"}])
        self.assertEqual(out[0], "Saved 3bhk-plan (floor_plan).")

    def test_hedge_on_hero(self):
        out = _saved_summary([{"role": "hero", "name": "hero"}])
        self.assertTrue(any("isn't from this project" in p for p in out))
        self.assertIn("the hero", out[-1])

    def test_hedge_lists_both_brand_roles(self):
        out = _saved_summary([{"role": "hero", "name": "h"}, {"role": "logo", "name": "l"}])
        self.assertIn("the hero or logo", out[-1])

    def test_no_hedge_on_plain_creative(self):
        out = _saved_summary([{"role": "amenity", "name": "pool"}])
        self.assertFalse(any("isn't from this project" in p for p in out))


if __name__ == "__main__":
    unittest.main()
