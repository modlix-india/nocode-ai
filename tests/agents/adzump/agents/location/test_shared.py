"""Unit: agents/location/tools/_shared.py - finalize_targets, the one ending of
every targeting change: map, save the areas on the product, repaint."""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.agents.location.tools import _shared


class FinalizeTargetsTests(unittest.TestCase):
    def _run(self, save):
        context = {"session_context": {"product_data": {"place": {"country_code": "IN"}},
                                       "campaign_spec": {"platform": "Meta"}}}
        mapper = mock.Mock(map_target_areas=mock.AsyncMock(return_value=[{"name": "Hebbal"}]))
        with mock.patch.object(_shared, "PlatformGeoMapper", return_value=mapper), \
             mock.patch.object(_shared, "save_product_fields", save), \
             mock.patch.object(_shared, "rerender_craft", mock.AsyncMock()):
            result = asyncio.run(_shared.finalize_targets([{"name": "Hebbal"}], context))
        return result, context["session_context"], save

    def test_the_mapped_areas_are_saved_on_the_product(self):
        result, session_ctx, save = self._run(mock.AsyncMock(return_value=True))
        self.assertEqual(result, [{"name": "Hebbal"}])
        self.assertEqual(save.await_args.args[2], {"target_areas": [{"name": "Hebbal"}]})
        self.assertTrue(session_ctx.get(_shared.GEO_FINALIZED_KEY))

    def test_a_failed_save_fails_the_run(self):
        with self.assertRaises(RuntimeError):
            self._run(mock.AsyncMock(side_effect=RuntimeError("db down")))


if __name__ == "__main__":
    unittest.main()
