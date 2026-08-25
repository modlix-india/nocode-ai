"""Unit tests for publishing a build to Google
(app/agents/adzump/agents/campaign/google/publish.py).
"""

# regression: partialFailure must be sent false so one bad operation fails the whole request
# and a campaign can never be half created; the dry-run flag must actually reach the request;
# and a Google failure carries its real cause in error.details, not in error.message.
from __future__ import annotations

import asyncio
import unittest
from datetime import date
from unittest import mock

from app.agents.adzump.adapters.google import campaigns
from app.agents.adzump.adapters.google.client import GoogleAdsApiError
from app.agents.adzump.agents.campaign.google import publish
from app.agents.adzump.agents.campaign.google.audience.constants import (
    BLUEPRINTS_KEY,
    pending_ref,
)
from app.agents.adzump.agents.campaign.models import audience, set_audience

_REF = "customers/1/userInterests/80001"
_CAMPAIGN = "customers/1/campaigns/777"
_PENDING = pending_ref("Buyers")
_CREATED = "customers/1/customAudiences/4242"


def _session(**spec):
    session_ctx = {
        "campaign_spec": {
            "platform": "GOOGLE",
            "account": "1",
            "channel": "Demand Gen",
            "budget": "₹10,000/day",
            "duration": "30 days",
            **spec,
        },
        "product_data": {
            "product_name": "Acme",
            "target_areas": [
                {
                    "name": "Bengaluru",
                    "google": {"resourceName": "geoTargetConstants/1007751"},
                }
            ],
        },
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
    return session_ctx


def _run(session_ctx, *, dry_run=False, response=None, error=None):
    sent = {}

    async def fake_post(endpoint, body, client_code, headers, login=None):
        sent["endpoint"] = endpoint
        sent["body"] = body
        if error is not None:
            raise error
        return response if response is not None else {"mutateOperationResponses": []}

    # Patched at the adapter's client, not at campaigns.mutate: what these pin is the
    # request that actually reaches Google.
    with (
        mock.patch.object(campaigns.google_ads_client, "post", new=fake_post),
        mock.patch.object(publish.settings, "ADZUMP_PUBLISH_DRY_RUN", dry_run),
    ):
        outcome = asyncio.run(publish.publish_campaign(session_ctx, {}))
    return outcome, sent


class RequestShapeTests(unittest.TestCase):
    def test_partial_failure_is_sent_false(self):
        # One bad operation must fail the whole request - no half-created campaign.
        _, sent = _run(_session())
        self.assertIs(sent["body"]["partialFailure"], False)

    def test_a_real_publish_does_not_send_validate_only(self):
        _, sent = _run(_session())
        self.assertNotIn("validateOnly", sent["body"])

    def test_the_dry_run_flag_reaches_the_request(self):
        outcome, sent = _run(_session(), dry_run=True)
        self.assertIs(sent["body"]["validateOnly"], True)
        self.assertTrue(outcome.dry_run)
        self.assertIn("nothing was created", outcome.message)

    def test_it_posts_to_the_atomic_endpoint(self):
        _, sent = _run(_session())
        self.assertEqual(sent["endpoint"], "customers/1/googleAds:mutate")


class GeoTargetTests(unittest.TestCase):
    def test_the_location_agents_constants_reach_the_payload(self):
        ctx = _session()
        ctx["product_data"]["target_areas"] = [
            {"name": "A", "google": {"resourceName": "geoTargetConstants/1"}},
            {"name": "B", "google": {"resourceName": "geoTargetConstants/2"}},
            # 19 neighbourhoods resolved to 18 postal-code constants live, so duplicates are
            # normal input - sending one twice fails the whole batch.
            {"name": "C", "google": {"resourceName": "geoTargetConstants/1"}},
            {"name": "D", "google": {}},
        ]
        _, sent = _run(ctx)
        geos = [
            o["adGroupCriterionOperation"]["create"]["location"]["geoTargetConstant"]
            for o in sent["body"]["mutateOperations"]
            if "adGroupCriterionOperation" in o
            and "location" in o["adGroupCriterionOperation"]["create"]
        ]
        self.assertEqual(geos, ["geoTargetConstants/1", "geoTargetConstants/2"])


class OutcomeTests(unittest.TestCase):
    def test_the_created_campaign_is_returned(self):
        outcome, _ = _run(
            _session(),
            response={
                "mutateOperationResponses": [
                    {"campaignBudgetResult": {"resourceName": "b"}},
                    {"campaignResult": {"resourceName": _CAMPAIGN}},
                ]
            },
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.campaign, _CAMPAIGN)

    def test_a_dry_run_reports_no_campaign(self):
        outcome, _ = _run(_session(), dry_run=True)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.campaign, "")

    def test_a_google_failure_surfaces_its_real_cause(self):
        # error.message is the generic "Request contains an invalid argument"; the cause is
        # in details, in one of two envelopes.
        outcome, _ = _run(
            _session(),
            error=GoogleAdsApiError(
                400,
                "Google Ads API 400: Request contains an invalid argument.",
                {
                    "error": {
                        "details": [
                            {
                                "errors": [
                                    {
                                        "errorCode": {
                                            "campaignBudgetError": "BUDGET_BELOW_PER_DAY_MINIMUM"
                                        },
                                        "message": "Too low.",
                                    }
                                ]
                            }
                        ]
                    }
                },
            ),
        )
        self.assertFalse(outcome.ok)
        self.assertIn("BUDGET_BELOW_PER_DAY_MINIMUM", outcome.message)


class GuardTests(unittest.TestCase):
    def test_a_channel_with_no_emitter_is_reported_not_attempted(self):
        outcome, _ = _run(_session(channel="Search"))
        self.assertFalse(outcome.ok)
        self.assertIn("cannot be created from here yet", outcome.message)

    def test_an_unbuilt_campaign_is_refused(self):
        session_ctx = {
            "campaign_spec": {"account": "1", "channel": "Demand Gen"},
            "product_data": {},
        }
        outcome, _ = _run(session_ctx)
        self.assertFalse(outcome.ok)
        self.assertIn("Nothing has been built", outcome.message)

    def test_a_missing_budget_is_reported_before_any_call(self):
        outcome, sent = _run(_session(budget=""))
        self.assertFalse(outcome.ok)
        self.assertIn("Could not assemble", outcome.message)
        self.assertEqual(sent, {})


class ConversionTests(unittest.TestCase):
    def test_budget_becomes_micros(self):
        self.assertEqual(publish._budget_micros("₹10,000/day"), 10_000_000_000)
        self.assertEqual(publish._budget_micros("$500/day"), 500_000_000)

    def test_duration_becomes_an_end_date(self):
        self.assertEqual(
            publish._end_date("30 days", today=date(2026, 8, 14)), "2026-09-13"
        )
        self.assertEqual(
            publish._end_date("1 month", today=date(2026, 8, 14)), "2026-09-13"
        )

    def test_no_start_date_is_sent(self):
        # "Today" in our clock can be yesterday in the account's time zone, which Google
        # rejects as a past start. Unset, Google starts the campaign itself.
        _, sent = _run(_session())
        campaign = next(
            o["campaignOperation"]["create"]
            for o in sent["body"]["mutateOperations"]
            if "campaignOperation" in o
        )
        self.assertNotIn("startDateTime", campaign)
        self.assertTrue(campaign["endDateTime"].endswith("23:59:59"))


class CustomSegmentTests(unittest.TestCase):
    """The approved-but-not-yet-created segment. Google will not take a CustomAudience inside
    the atomic mutate, so publish creates it first and swaps the pending ref for the real one.
    """

    def _pending(self, *, alone=False):
        session_ctx = _session()
        dump = audience(session_ctx)
        dump["signals"].append(
            {
                "kind": "CUSTOM_AUDIENCE",
                "ref": _PENDING,
                "label": "Buyers",
                "source": "GENERATED",
                "rationale": "",
                "path": [],
                "negative": False,
                "owned": True,
                "metrics": None,
            }
        )
        if alone:
            dump["signals"] = [s for s in dump["signals"] if s["ref"] != _REF]
            dump["dimension_groups"] = [[_PENDING]]
        else:
            dump["dimension_groups"] = [[_REF, _PENDING]]
        set_audience(session_ctx, dump)
        session_ctx[BLUEPRINTS_KEY] = {
            _PENDING: {
                "label": "Buyers",
                "terms": [{"keyword": "k", "volume": 10}],
                "urls": [],
                "apps": [],
            }
        }
        return session_ctx

    def _live(self, session_ctx):
        async def fake_list(**_):
            return []

        async def fake_create(**_):
            return _CREATED

        with (
            mock.patch.object(publish.custom_audience, "list_enabled", new=fake_list),
            mock.patch.object(publish.custom_audience, "create", new=fake_create),
        ):
            return _run(session_ctx, response={"mutateOperationResponses": []})

    def test_a_created_segment_replaces_its_pending_ref_in_the_session(self):
        # The build publish emits from is a COPY, so a swap made only there leaves the session
        # holding a ref for a segment that now exists - and the next launch creates a second.
        session_ctx = self._pending()
        outcome, sent = self._live(session_ctx)

        self.assertTrue(outcome.ok)
        refs = [s["ref"] for s in audience(session_ctx)["signals"]]
        self.assertEqual(refs, [_REF, _CREATED])
        self.assertEqual(audience(session_ctx)["dimension_groups"], [[_REF, _CREATED]])
        # Re-keyed, not dropped - the panel still shows what the segment is made of.
        self.assertNotIn(_PENDING, session_ctx[BLUEPRINTS_KEY])
        self.assertEqual(session_ctx[BLUEPRINTS_KEY][_CREATED]["label"], "Buyers")
        # The real ref, not the pending one, is what Google was asked to target.
        self.assertIn(_CREATED, str(sent["body"]))
        self.assertNotIn(_PENDING, str(sent["body"]))

    def test_a_dry_run_creates_nothing_and_leaves_the_session_alone(self):
        session_ctx = self._pending()
        outcome, sent = _run(session_ctx, dry_run=True)

        self.assertTrue(outcome.ok)
        self.assertIn("Buyers", outcome.message)
        # Still pending, still holding its terms: nothing was created to swap in.
        self.assertIn(_PENDING, [s["ref"] for s in audience(session_ctx)["signals"]])
        self.assertIn(_PENDING, session_ctx[BLUEPRINTS_KEY])
        self.assertNotIn(_PENDING, str(sent["body"]))

    def test_a_dry_run_refuses_when_the_custom_segment_is_the_only_audience(self):
        # Dropping it would leave nothing to target, and validating what remains says nothing
        # about the campaign the user built.
        session_ctx = self._pending(alone=True)
        outcome, sent = _run(session_ctx, dry_run=True)

        self.assertFalse(outcome.ok)
        self.assertIn("ADZUMP_PUBLISH_DRY_RUN=false", outcome.message)
        self.assertEqual(sent, {})

    def test_a_missing_blueprint_stops_the_launch(self):
        session_ctx = self._pending()
        session_ctx[BLUEPRINTS_KEY] = {}
        outcome, sent = self._live(session_ctx)

        self.assertFalse(outcome.ok)
        self.assertIn("terms are missing", outcome.message)
        self.assertEqual(sent, {})


if __name__ == "__main__":
    unittest.main()
