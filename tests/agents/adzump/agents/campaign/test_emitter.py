"""Unit tests for the Demand Gen emitter
(app/agents/adzump/agents/campaign/google/emitter/).

The payload this builds was validated against a live account with validateOnly, so these
tests pin the shape that was accepted - not a shape we believe is right.
"""

# regression: `maps` is a v24 field and the client speaks v23, so sending it failed the whole
# mutate with Unknown name "maps"; the schedule fields are start_date_time / end_date_time and
# the END must be 23:59:59; biddingStrategyType is OUTPUT_ONLY and must never be sent; an
# open-ended age band must OMIT maxAge rather than send null; and errors arrive in two
# different envelopes, so a handler reading one shows nothing for the other.
from __future__ import annotations

import unittest

from app.agents.adzump.agents.campaign.google.emitter import (
    MICROS,
    as_campaign_datetime,
    parse_mutate_errors,
)
from app.agents.adzump.agents.campaign.google.channel_controls import SURFACES
from app.agents.adzump.agents.campaign.google.emitter.demand_gen import (
    audience_exclusions,
)
from app.agents.adzump.agents.campaign.google.emitter.demand_gen import (
    operations as demand_gen_operations,
)

CID = "8928324049"
_INTEREST = f"customers/{CID}/userInterests/80001"
_LIFE_EVENT = f"customers/{CID}/lifeEvents/95001"
_LIST = f"customers/{CID}/userLists/9"


def _sig(kind, ref, negative=False):
    return {
        "kind": kind,
        "ref": ref,
        "label": "x",
        "source": "TAXONOMY",
        "rationale": "",
        "path": [],
        "negative": negative,
        "owned": False,
        "metrics": None,
    }


def _dump(signals=None, demographics=None, groups=None):
    signals = signals or [_sig("IN_MARKET", _INTEREST)]
    positives = [s["ref"] for s in signals if not s["negative"]]
    return {
        "signals": signals,
        "demographics": demographics or {},
        "dimension_groups": groups if groups is not None else [positives],
        "meta": {},
    }


def _ops(**over):
    kwargs = {
        "customer_id": CID,
        "campaign_name": "Campaign",
        "budget_micros": 1000 * MICROS,
        "build": {"audience": _dump()},
        "product_name": "probe",
    }
    if "build_audience" in over:
        kwargs["build"] = {"audience": over.pop("build_audience")}
    kwargs.update(over)
    return demand_gen_operations(**kwargs)


def _op(ops, key):
    return next(o[key]["create"] for o in ops if key in o)


class StructureTests(unittest.TestCase):
    def test_five_operations_in_dependency_order(self):
        self.assertEqual(
            [next(iter(o)) for o in _ops()],
            [
                "campaignBudgetOperation",
                "campaignOperation",
                "audienceOperation",
                "adGroupOperation",
                "adGroupCriterionOperation",
            ],
        )

    def test_temporary_ids_thread_across_operations(self):
        # Each later operation must name the earlier one's temp resource, or the atomic
        # request resolves nothing and every dependent op reports RESOURCE_NOT_FOUND.
        ops = _ops()
        budget = _op(ops, "campaignBudgetOperation")
        campaign = _op(ops, "campaignOperation")
        aud = _op(ops, "audienceOperation")
        ad_group = _op(ops, "adGroupOperation")
        criterion = _op(ops, "adGroupCriterionOperation")

        self.assertEqual(campaign["campaignBudget"], budget["resourceName"])
        self.assertEqual(ad_group["campaign"], campaign["resourceName"])
        self.assertEqual(criterion["adGroup"], ad_group["resourceName"])
        self.assertEqual(criterion["audience"]["audience"], aud["resourceName"])

    def test_grouped_mode_is_set_and_is_a_sibling(self):
        # IMMUTABLE: wrong at create means deleting and rebuilding the ad group. And it sits
        # on AdGroup, NOT inside demandGenAdGroupSettings - the documented easy mistake.
        ad_group = _op(_ops(), "adGroupOperation")
        self.assertIs(ad_group["audienceSetting"]["useAudienceGrouped"], True)
        self.assertNotIn("audienceSetting", ad_group["demandGenAdGroupSettings"])

    def test_campaign_is_created_paused(self):
        # There is no ad until creative lands; an enabled campaign that cannot serve is
        # worse than an obviously unfinished one.
        self.assertEqual(_op(_ops(), "campaignOperation")["status"], "PAUSED")

    def test_the_eu_political_declaration_is_always_sent(self):
        # Omitting it returns fieldError REQUIRED - verified live.
        campaign = _op(_ops(), "campaignOperation")
        self.assertIn("containsEuPoliticalAdvertising", campaign)

    def test_output_only_fields_are_never_sent(self):
        # biddingStrategyType is OUTPUT_ONLY. The API accepts it silently, which is worse
        # than rejecting it, so nothing downstream would tell us.
        campaign = _op(_ops(), "campaignOperation")
        self.assertNotIn("biddingStrategyType", campaign)
        self.assertEqual(campaign["targetSpend"], {})
        self.assertNotIn("advertisingChannelSubType", campaign)  # Demand Gen has none

    def test_budget_is_not_shared(self):
        # Demand Gen budgets cannot be shared, unlike Search.
        self.assertIs(_op(_ops(), "campaignBudgetOperation")["explicitlyShared"], False)


class ScheduleTests(unittest.TestCase):
    """Campaign scheduling is start_date_time / end_date_time, and the two ends differ."""

    def test_the_field_names_are_date_time_not_date(self):
        # startDate / endDate are rejected as unknown names.
        campaign = _op(
            _ops(start_date="2026-09-01", end_date="2026-10-01"), "campaignOperation"
        )
        self.assertIn("startDateTime", campaign)
        self.assertNotIn("startDate", campaign)
        self.assertNotIn("endDate", campaign)

    def test_start_is_midnight_and_end_is_the_last_second(self):
        # An end of 00:00:00 fails with END_TIME_MUST_BE_THE_END_OF_A_DAY. The proto only
        # documents the 00:00:00 half.
        campaign = _op(
            _ops(start_date="2026-09-01", end_date="2026-10-01"), "campaignOperation"
        )
        self.assertEqual(campaign["startDateTime"], "2026-09-01 00:00:00")
        self.assertEqual(campaign["endDateTime"], "2026-10-01 23:59:59")

    def test_no_dates_means_no_date_fields(self):
        campaign = _op(_ops(), "campaignOperation")
        self.assertNotIn("startDateTime", campaign)
        self.assertNotIn("endDateTime", campaign)

    def test_a_full_datetime_is_passed_through(self):
        self.assertEqual(
            as_campaign_datetime("2026-09-01 08:30:00"), "2026-09-01 08:30:00"
        )


class SurfaceTests(unittest.TestCase):
    def test_maps_is_never_sent(self):
        # v24 field; the client speaks v23, where it fails the whole mutate.
        self.assertNotIn("maps", {s.key for s in SURFACES})
        controls = _op(_ops(), "adGroupOperation")["demandGenAdGroupSettings"][
            "channelControls"
        ]
        self.assertNotIn("maps", controls["selectedChannels"])

    def test_in_stream_is_off_for_image_creative(self):
        # Image ads cannot serve on YouTube in-stream, so enabling it is dead reach.
        controls = _op(_ops(), "adGroupOperation")["demandGenAdGroupSettings"][
            "channelControls"
        ]
        self.assertIs(controls["selectedChannels"]["youtubeInStream"], False)
        self.assertIs(controls["selectedChannels"]["gmail"], True)

    def test_surfaces_are_a_oneof_never_both(self):
        controls = _op(_ops(), "adGroupOperation")["demandGenAdGroupSettings"][
            "channelControls"
        ]
        self.assertIn("selectedChannels", controls)
        self.assertNotIn("channelStrategy", controls)
        self.assertNotIn("channelConfig", controls)  # output only


class DimensionTests(unittest.TestCase):
    def _dims(self, **over):
        return _op(_ops(**over), "audienceOperation")["dimensions"]

    def test_every_signal_kind_maps_to_its_segment_field(self):
        pairs = [
            ("IN_MARKET", _INTEREST, "userInterest", "userInterestCategory"),
            ("AFFINITY", _INTEREST, "userInterest", "userInterestCategory"),
            ("LIFE_EVENT", _LIFE_EVENT, "lifeEvent", "lifeEvent"),
            (
                "DETAILED_DEMOGRAPHIC",
                f"customers/{CID}/detailedDemographics/1",
                "detailedDemographic",
                "detailedDemographic",
            ),
            (
                "CUSTOM_AUDIENCE",
                f"customers/{CID}/customAudiences/5",
                "customAudience",
                "customAudience",
            ),
            ("USER_LIST", _LIST, "userList", "userList"),
        ]
        for kind, ref, outer, inner in pairs:
            dims = self._dims(build_audience=_dump([_sig(kind, ref)]))
            segment = dims[0]["audienceSegments"]["segments"][0]
            self.assertEqual(segment, {outer: {inner: ref}}, kind)

    def test_positives_share_one_segment_dimension(self):
        # Dimensions AND together; splitting the positives would intersect rather than
        # union them, narrowing the audience.
        dump = _dump([_sig("IN_MARKET", _INTEREST), _sig("LIFE_EVENT", _LIFE_EVENT)])
        dims = self._dims(build_audience=dump)
        segment_dims = [d for d in dims if "audienceSegments" in d]
        self.assertEqual(len(segment_dims), 1)
        self.assertEqual(len(segment_dims[0]["audienceSegments"]["segments"]), 2)

    def test_an_open_ended_age_band_omits_max_age(self):
        # Sending null is not the same as leaving it unset.
        dump = _dump(demographics={"age_ranges": [{"min_age": 65}]})
        age = next(d for d in self._dims(build_audience=dump) if "age" in d)["age"]
        self.assertEqual(age["ageRanges"], [{"minAge": 65}])

    def test_age_is_sent_as_integers(self):
        # The AgeRangeType enum returns "Invalid value ... (TYPE_INT32)".
        dump = _dump(demographics={"age_ranges": [{"min_age": 25, "max_age": 54}]})
        age = next(d for d in self._dims(build_audience=dump) if "age" in d)["age"]
        self.assertEqual(age["ageRanges"], [{"minAge": 25, "maxAge": 54}])

    def test_each_demographic_becomes_its_own_dimension(self):
        dump = _dump(
            demographics={
                "age_ranges": [{"min_age": 25, "max_age": 54}],
                "genders": ["FEMALE"],
                "income_ranges": ["INCOME_RANGE_90_UP"],
                "parental_statuses": ["PARENT"],
            }
        )
        keys = [next(iter(d)) for d in self._dims(build_audience=dump)]
        self.assertEqual(
            keys,
            ["audienceSegments", "age", "gender", "householdIncome", "parentalStatus"],
        )

    def test_empty_demographics_add_no_dimensions(self):
        self.assertEqual([next(iter(d)) for d in self._dims()], ["audienceSegments"])

    def test_an_audience_with_no_dimension_is_refused(self):
        # Grouped mode has no untargeted fallback - an ad group must target something.
        with self.assertRaises(ValueError):
            _ops(build_audience=_dump(signals=[], groups=[]))


class ExclusionTests(unittest.TestCase):
    def test_only_user_lists_are_excludable(self):
        from app.agents.adzump.agents.campaign.google.audience.models import (
            AudienceTargetingResult,
        )

        dump = _dump(
            [_sig("IN_MARKET", _INTEREST), _sig("USER_LIST", _LIST, negative=True)]
        )
        result = AudienceTargetingResult.model_validate(dump)
        self.assertEqual(
            audience_exclusions(result),
            {"exclusions": [{"userList": {"userList": _LIST}}]},
        )

    def test_no_exclusions_means_no_exclusion_dimension(self):
        self.assertNotIn("exclusionDimension", _op(_ops(), "audienceOperation"))

    def test_an_excluded_list_is_not_also_targeted(self):
        dump = _dump(
            [_sig("IN_MARKET", _INTEREST), _sig("USER_LIST", _LIST, negative=True)]
        )
        segments = _op(_ops(build_audience=dump), "audienceOperation")["dimensions"][0][
            "audienceSegments"
        ]["segments"]
        self.assertEqual(
            segments, [{"userInterest": {"userInterestCategory": _INTEREST}}]
        )


class ErrorEnvelopeTests(unittest.TestCase):
    """Google answers in two shapes; reading one shows nothing for the other."""

    def test_google_ads_failure_envelope(self):
        payload = {
            "error": {
                "details": [
                    {
                        "errors": [
                            {
                                "errorCode": {"audienceError": "DIMENSION_INVALID"},
                                "message": "A dimension is not valid.",
                            }
                        ]
                    }
                ]
            }
        }
        self.assertEqual(
            parse_mutate_errors(payload),
            ["DIMENSION_INVALID: A dimension is not valid."],
        )

    def test_field_violation_envelope(self):
        payload = {
            "error": {
                "details": [
                    {
                        "fieldViolations": [
                            {
                                "field": "operations[0].create.type",
                                "description": 'Unknown name "maps"',
                            }
                        ]
                    }
                ]
            }
        }
        self.assertEqual(
            parse_mutate_errors(payload),
            ['operations[0].create.type: Unknown name "maps"'],
        )

    def test_the_first_error_is_kept_first(self):
        # In a failed atomic build the later RESOURCE_NOT_FOUNDs are symptoms of the
        # operation that actually failed.
        payload = {
            "error": {
                "details": [
                    {
                        "errors": [
                            {
                                "errorCode": {
                                    "contextError": "OPERATION_NOT_PERMITTED_FOR_CONTEXT"
                                },
                                "message": "cause",
                            },
                            {
                                "errorCode": {"mutateError": "RESOURCE_NOT_FOUND"},
                                "message": "symptom",
                            },
                        ]
                    }
                ]
            }
        }
        self.assertEqual(
            parse_mutate_errors(payload)[0],
            "OPERATION_NOT_PERMITTED_FOR_CONTEXT: cause",
        )

    def test_a_bare_message_still_surfaces(self):
        self.assertEqual(
            parse_mutate_errors({"error": {"message": "UNAUTHENTICATED"}}),
            ["UNAUTHENTICATED"],
        )


if __name__ == "__main__":
    unittest.main()
