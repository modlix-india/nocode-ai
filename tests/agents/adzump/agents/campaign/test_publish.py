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

from app.agents.adzump.adapters.google.client import GoogleAdsApiError
from app.agents.adzump.agents.campaign.google import publish
from app.agents.adzump.agents.campaign.models import set_audience

_REF = "customers/1/userInterests/80001"
_CAMPAIGN = "customers/1/campaigns/777"


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
        "product_data": {"product_name": "Acme"},
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

    with (
        mock.patch.object(publish.google_ads_client, "post", new=fake_post),
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


if __name__ == "__main__":
    unittest.main()
