"""Unit tests for the publish step inside launch_campaign
(app/agents/adzump/tools/launch.py).
"""

# regression: publish runs BEFORE save, so a failed publish must leave campaign_status
# untouched and stay retryable; a dry run must not claim a launch; a channel with no emitter
# must keep its pre-posting behaviour; and a save failure AFTER a successful publish must not
# tell the user to retry, because the campaign already exists.
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.agents.campaign.google.publish import PublishOutcome
from app.agents.adzump.agents.campaign.models import set_audience
from app.agents.adzump.tools import launch

_CAMPAIGN = "customers/1/campaigns/777"


def _ctx():
    ctx = {
        "session_context": {
            "campaign_spec": {
                "platform": "GOOGLE",
                "channel": "Demand Gen",
                "duration": "30 days",
                "budget": "₹10,000/day",
                "parent_account": "9",
                "account": "1",
            },
            "product_data": {"product_name": "Acme"},
        },
        "session_id": "s1",
    }
    # A launch is refused unless the channel's build ran - so these fixtures carry one.
    set_audience(
        ctx["session_context"],
        {
            "signals": [
                {
                    "kind": "AFFINITY",
                    "ref": "customers/1/userInterests/1",
                    "label": "Home Buyers",
                    "source": "TAXONOMY",
                    "rationale": "r",
                    "path": ["Home Buyers"],
                }
            ],
            "demographics": {},
            "dimension_groups": [["customers/1/userInterests/1"]],
            "meta": {},
        },
    )
    return ctx


def _run(ctx, outcome, *, saved="rec-1", consent="yes launch"):
    async def fake_publish(session_ctx, context):
        return outcome

    async def fake_save(session_ctx, context):
        return saved

    with (
        mock.patch.object(launch, "publish_campaign", new=fake_publish),
        mock.patch.object(launch, "save_campaign", new=fake_save),
        mock.patch.object(launch, "_last_user_text", return_value=consent),
        mock.patch.object(launch, "resolve_url", return_value="https://x.com"),
    ):
        return asyncio.run(launch._launch_campaign({}, ctx))


class BuildGateTests(unittest.TestCase):
    """Seen live: "Yes, proceed" on the SUMMARY chip reads to the consent gate exactly like
    "Yes, launch", and a campaign with no keywords and no audience was recorded as launched."""

    def test_a_google_campaign_with_no_build_cannot_launch(self):
        ctx = _ctx()
        ctx["session_context"].pop("campaign_build", None)
        res = _run(ctx, PublishOutcome(True, "ok", campaign=_CAMPAIGN), consent="Yes, proceed")
        self.assertFalse(res.success)
        self.assertIn("not been built", res.error)
        # nothing recorded, so the user can still build and launch properly
        self.assertNotIn("campaign_status", ctx["session_context"]["campaign_spec"])

    def test_a_built_campaign_still_launches_on_the_same_words(self):
        res = _run(_ctx(), PublishOutcome(True, "ok", campaign=_CAMPAIGN), consent="Yes, proceed")
        self.assertTrue(res.success, res.error)

    def test_a_save_failure_with_nothing_published_stays_retryable(self):
        # An unsupported channel publishes NOTHING, so the "it WAS created, do not retry"
        # message was a false claim about an irreversible action.
        ctx = _ctx()
        res = _run(ctx, PublishOutcome(False, "no emitter", supported=False), saved=None)
        self.assertFalse(res.success)
        self.assertIn("safe to try again", res.error)
        self.assertNotIn("WAS created", res.error)
        self.assertIsNone(ctx["session_context"]["campaign_spec"].get("campaign_status"))

    def test_a_timeout_blocks_the_retry_that_would_duplicate(self):
        # Google may already hold the campaign; creating is at-most-once.
        ctx = _ctx()
        res = _run(ctx, PublishOutcome(False, "timed out", uncertain=True))
        self.assertFalse(res.success)
        self.assertEqual(
            ctx["session_context"]["campaign_spec"]["campaign_status"], "launched"
        )
        self.assertTrue(ctx["session_context"]["launched_campaign"])

    def test_a_campaign_with_no_resource_name_still_blocks_a_retry(self):
        # "" is falsy, so the idempotency guard would not have fired on it.
        ctx = _ctx()
        _run(ctx, PublishOutcome(True, "ok", campaign=""), saved=None)
        self.assertTrue(ctx["session_context"]["launched_campaign"])

    def test_meta_has_no_build_stage_and_is_not_blocked(self):
        ctx = _ctx()
        ctx["session_context"].pop("campaign_build", None)
        spec = ctx["session_context"]["campaign_spec"]
        spec["platform"], spec["fb_page"] = "META", "F"
        spec.pop("channel")
        res = _run(ctx, PublishOutcome(True, "n/a", supported=False))
        self.assertTrue(res.success, res.error)


class PublishOrderTests(unittest.TestCase):
    def test_a_failed_publish_saves_nothing_and_stays_retryable(self):
        ctx = _ctx()
        res = _run(ctx, PublishOutcome(False, "Google rejected the campaign: BAD"))
        self.assertFalse(res.success)
        self.assertIn("BAD", res.error)
        # untouched, so the idempotency guard does not trip on the retry
        self.assertNotIn("campaign_status", ctx["session_context"]["campaign_spec"])
        self.assertNotIn("product_id", ctx["session_context"])

    def test_a_successful_publish_then_saves_and_marks_launched(self):
        ctx = _ctx()
        res = _run(ctx, PublishOutcome(True, "Campaign created, paused.", _CAMPAIGN))
        self.assertTrue(res.success)
        self.assertEqual(
            ctx["session_context"]["campaign_spec"]["campaign_status"], "launched"
        )
        self.assertEqual(ctx["session_context"]["product_id"], "rec-1")

    def test_consent_is_still_required_before_publishing(self):
        published = []

        async def fake_publish(session_ctx, context):
            published.append(1)
            return PublishOutcome(True, "created", _CAMPAIGN)

        ctx = _ctx()
        with (
            mock.patch.object(launch, "publish_campaign", new=fake_publish),
            mock.patch.object(launch, "_last_user_text", return_value="not yet"),
        ):
            res = asyncio.run(launch._launch_campaign({}, ctx))
        self.assertFalse(res.success)
        self.assertEqual(published, [])  # the gate runs before the side effect


class DryRunTests(unittest.TestCase):
    def test_a_dry_run_does_not_claim_a_launch(self):
        ctx = _ctx()
        res = _run(
            ctx,
            PublishOutcome(
                True, "Validated 5 operations - nothing was created.", dry_run=True
            ),
        )
        self.assertTrue(res.success)
        self.assertTrue(res.data["dry_run"])
        self.assertIn("NOT created", res.summary)
        # nothing recorded, so the run can be repeated
        self.assertNotIn("campaign_status", ctx["session_context"]["campaign_spec"])
        self.assertNotIn("product_id", ctx["session_context"])


class UnsupportedChannelTests(unittest.TestCase):
    def test_a_channel_with_no_emitter_keeps_the_old_behaviour(self):
        # Search has no emitter yet; it must still save rather than read as a failed launch.
        ctx = _ctx()
        ctx["session_context"]["campaign_spec"]["channel"] = "Search"
        res = _run(
            ctx,
            PublishOutcome(False, "SEARCH cannot be created yet.", supported=False),
        )
        self.assertTrue(res.success)
        self.assertEqual(
            ctx["session_context"]["campaign_spec"]["campaign_status"], "launched"
        )


class SaveFailureTests(unittest.TestCase):
    def test_a_save_failure_after_publish_warns_against_retrying(self):
        # The campaign already exists - "retry" would create a second one.
        ctx = _ctx()
        res = _run(
            ctx,
            PublishOutcome(True, "Campaign created, paused.", _CAMPAIGN),
            saved=None,
        )
        self.assertFalse(res.success)
        self.assertIn("WAS created", res.error)
        self.assertIn(_CAMPAIGN, res.error)
        self.assertIn("do NOT launch again", res.error)


if __name__ == "__main__":
    unittest.main()
