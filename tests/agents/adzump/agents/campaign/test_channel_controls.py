"""Unit tests for Demand Gen channel controls
(google/channel_controls.py, tools/google/channel_controls.py, and the craft block).
"""

# regression: image ads cannot serve on YouTube in-stream and video ads cannot serve on
# Gmail, so a selection saved under one ad type must not keep enabling what the other cannot
# use; and every surface off would leave the ad nowhere to show.
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.agents.campaign.craft import channel_controls_block
from app.agents.adzump.agents.campaign.google import channel_controls as cc
from app.agents.adzump.agents.campaign.google.emitter.demand_gen import (
    operations as demand_gen_operations,
)
from app.agents.adzump.agents.campaign.models import (
    channel_controls as saved_controls,
)
from app.agents.adzump.agents.campaign.models import (
    set_audience,
    set_channel_controls,
)
from app.agents.adzump.agents.campaign.tools.google import (
    channel_controls as tool,
)

IMAGE = cc.AdType.IMAGE
VIDEO = cc.AdType.VIDEO
_REF = "customers/1/userInterests/80001"


def _ctx(channel="Demand Gen"):
    session_ctx = {
        "campaign_spec": {"platform": "GOOGLE", "account": "1", "channel": channel}
    }
    set_audience(
        session_ctx,
        {
            "signals": [
                {
                    "kind": "IN_MARKET",
                    "ref": _REF,
                    "label": "x",
                    "source": "TAXONOMY",
                    "rationale": "",
                    "path": [],
                    "negative": False,
                    "owned": False,
                    "metrics": None,
                }
            ],
            "demographics": {},
            "dimension_groups": [[_REF]],
            "meta": {},
        },
    )
    return {"session_context": session_ctx, "event_stream": mock.AsyncMock()}


class EligibilityTests(unittest.TestCase):
    """The matrix is Google's, verified from the image and video asset spec pages."""

    def test_image_cannot_serve_in_stream(self):
        self.assertFalse(cc.defaults(IMAGE)["youtubeInStream"])
        self.assertTrue(cc.defaults(IMAGE)["gmail"])

    def test_video_cannot_serve_gmail(self):
        self.assertTrue(cc.defaults(VIDEO)["youtubeInStream"])
        self.assertFalse(cc.defaults(VIDEO)["gmail"])

    def test_the_exclusions_are_mirror_images(self):
        # Neither format alone covers every surface.
        self.assertNotEqual(cc.defaults(IMAGE), cc.defaults(VIDEO))

    def test_maps_is_not_a_surface(self):
        # v24 field; the client speaks v23, where sending it fails the whole mutate.
        self.assertNotIn("maps", {s.key for s in cc.SURFACES})

    def test_a_locked_surface_explains_itself(self):
        in_stream = next(s for s in cc.SURFACES if s.key == "youtubeInStream")
        self.assertIn("image ads", cc.locked_reason(in_stream, IMAGE))
        self.assertEqual(cc.locked_reason(in_stream, VIDEO), "")


class ToggleTests(unittest.TestCase):
    def test_turning_off_a_surface_sticks(self):
        updated, err = cc.toggle(None, "gmail", False, IMAGE)
        self.assertEqual(err, "")
        self.assertFalse(updated["gmail"])
        self.assertTrue(updated["discover"])

    def test_an_ineligible_surface_cannot_be_turned_on(self):
        updated, err = cc.toggle(None, "youtubeInStream", True, IMAGE)
        self.assertIsNone(updated)
        self.assertIn("cannot serve here", err)

    def test_an_unknown_surface_is_refused(self):
        updated, err = cc.toggle(None, "tiktok", True, IMAGE)
        self.assertIsNone(updated)
        self.assertIn("not a Demand Gen surface", err)

    def test_the_last_surface_cannot_be_turned_off(self):
        only_gmail = {k: False for k in cc.defaults(IMAGE)}
        only_gmail["gmail"] = True
        updated, err = cc.toggle(only_gmail, "gmail", False, IMAGE)
        self.assertIsNone(updated)
        self.assertIn("needs somewhere to show", err)

    def test_an_ineligible_surface_is_forced_off_on_read(self):
        # Saved under video (in-stream on); read back under image, where it cannot serve.
        corrected = cc.normalize(cc.defaults(VIDEO), IMAGE)
        self.assertFalse(corrected["youtubeInStream"])
        # A surface stored off stays off. normalize cannot tell "the user turned it off" from
        # "the previous ad type could not use it", so it keeps the safer of the two. Only
        # reachable if the ad type ever changes mid-campaign; the user can turn it back on.
        self.assertFalse(corrected["gmail"])


class BuildToolTests(unittest.TestCase):
    def test_it_writes_the_defaults_for_the_ad_type(self):
        ctx = _ctx()
        res = asyncio.run(tool._channel_controls({}, ctx))
        self.assertTrue(res.success)
        self.assertEqual(saved_controls(ctx["session_context"]), cc.defaults(IMAGE))

    def test_search_campaigns_skip_it(self):
        ctx = _ctx(channel="Search")
        res = asyncio.run(tool._channel_controls({}, ctx))
        self.assertTrue(res.success)
        self.assertTrue(res.data["skipped"])

    def test_the_panel_toggle_persists(self):
        ctx = _ctx()
        asyncio.run(tool._channel_controls({}, ctx))
        res = asyncio.run(
            tool.update_channel_controls({"surface": "gmail", "enabled": False}, ctx)
        )
        self.assertTrue(res.success)
        self.assertFalse(saved_controls(ctx["session_context"])["gmail"])

    def test_the_panel_cannot_enable_an_ineligible_surface(self):
        ctx = _ctx()
        asyncio.run(tool._channel_controls({}, ctx))
        res = asyncio.run(
            tool.update_channel_controls(
                {"surface": "youtubeInStream", "enabled": True}, ctx
            )
        )
        self.assertFalse(res.success)
        self.assertIn("cannot serve here", res.error)


class ChannelGuardTests(unittest.TestCase):
    """The widget arrives as a chat message, so it can land on ANY campaign."""

    def test_a_toggle_on_a_search_campaign_cannot_destroy_the_build(self):
        # set_channel_controls writes a DEMAND_GEN slot, which replaces a Search build
        # wholesale - so without a guard here a stray widget message deletes the keywords.
        from app.agents.adzump.agents.campaign.models import (
            keyword_research,
            set_keyword_research,
        )

        session_ctx = {
            "campaign_spec": {"platform": "GOOGLE", "account": "1", "channel": "Search"}
        }
        set_keyword_research(session_ctx, {"themes": {"brand": {}}, "meta": {}})
        ctx = {"session_context": session_ctx, "event_stream": mock.AsyncMock()}

        res = asyncio.run(
            tool.update_channel_controls({"surface": "gmail", "enabled": False}, ctx)
        )
        self.assertFalse(res.success)
        self.assertIsNotNone(keyword_research(session_ctx))


class CraftBlockTests(unittest.TestCase):
    def test_every_surface_is_listed_including_the_locked_one(self):
        # Hiding it would leave the user wondering where YouTube in-stream went.
        block = channel_controls_block(None, IMAGE)
        by_key = {r["surface"]: r for r in block["rows"]}
        self.assertEqual(len(by_key), len(cc.SURFACES))
        self.assertTrue(by_key["youtubeInStream"]["locked"])
        self.assertIn("image ads", by_key["youtubeInStream"]["reason"])
        self.assertFalse(by_key["gmail"]["locked"])

    def test_it_names_the_ad_type(self):
        self.assertEqual(channel_controls_block(None, IMAGE)["ad_type"], "Image ads")


class EmitterReadsTheSlotTests(unittest.TestCase):
    def _selected(self, session_ctx):
        from app.agents.adzump.agents.campaign.models import build_dump

        block = (build_dump(session_ctx) or {})["demand_gen"]
        ops = demand_gen_operations(
            customer_id="1",
            campaign_name="C",
            budget_micros=1,
            build=block,
            geo_targets=["geoTargetConstants/1007751"],
        )
        return ops[3]["adGroupOperation"]["create"]["demandGenAdGroupSettings"][
            "channelControls"
        ]["selectedChannels"]

    def test_the_users_edit_reaches_the_payload(self):
        ctx = _ctx()
        updated, _ = cc.toggle(None, "gmail", False, IMAGE)
        set_channel_controls(ctx["session_context"], updated)
        self.assertFalse(self._selected(ctx["session_context"])["gmail"])

    def test_an_unset_slot_falls_back_to_the_defaults(self):
        # A campaign built before the tool existed must still post correctly.
        ctx = _ctx()
        self.assertEqual(self._selected(ctx["session_context"]), cc.defaults(IMAGE))

    def test_channel_strategy_is_never_sent_alongside(self):
        # oneof: selectedChannels or channelStrategy, never both.
        ctx = _ctx()
        from app.agents.adzump.agents.campaign.models import build_dump

        block = (build_dump(ctx["session_context"]) or {})["demand_gen"]
        ops = demand_gen_operations(
            customer_id="1",
            campaign_name="C",
            budget_micros=1,
            build=block,
            geo_targets=["geoTargetConstants/1007751"],
        )
        controls = ops[3]["adGroupOperation"]["create"]["demandGenAdGroupSettings"][
            "channelControls"
        ]
        self.assertNotIn("channelStrategy", controls)
        self.assertNotIn("channelConfig", controls)


if __name__ == "__main__":
    unittest.main()
