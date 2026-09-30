"""Demand Gen -> the atomic googleAds:mutate operations.

Five operations, in dependency order: budget, campaign, audience, ad group, criterion.
Temporary negative ids thread across them, which is why the emitter is channel-level rather
than per-slot - no single slot can own a fragment of it.

Shape validated live with validateOnly on a client account; the traps it caught are in the
regression note on tests/agents/adzump/agents/campaign/test_emitter.py.
"""

from __future__ import annotations

from typing import Any

from app.agents.adzump.agents.campaign.google.audience.constants import is_pending
from app.agents.adzump.agents.campaign.google.audience.models import (
    AudienceTargetingResult,
    SignalKind,
)
from app.agents.adzump.agents.campaign.google.channel_controls import ad_type_for
from app.agents.adzump.agents.campaign.google.channel_controls import (
    normalize as normalize_controls,
)
from app.agents.adzump.agents.campaign.google.emitter.shared import (
    TempIds,
    as_campaign_datetime,
    budget_operation,
    campaign_operation,
)

CHANNEL_TYPE = "DEMAND_GEN"

# Maximize Clicks - the only strategy needing no conversion history, which is the state a new
# advertiser's account is in. TargetSpend.target_spend_micros is proto-deprecated, so the
# empty object is the current correct usage.
BIDDING: dict[str, Any] = {"targetSpend": {}}

# SignalKind -> the AudienceSegment variant it becomes. IN_MARKET and AFFINITY are both
# user_interest; they differ in meaning, not in how they emit.
_SEGMENT_FIELD = {
    SignalKind.IN_MARKET: ("userInterest", "userInterestCategory"),
    SignalKind.AFFINITY: ("userInterest", "userInterestCategory"),
    SignalKind.LIFE_EVENT: ("lifeEvent", "lifeEvent"),
    SignalKind.DETAILED_DEMOGRAPHIC: ("detailedDemographic", "detailedDemographic"),
    SignalKind.CUSTOM_AUDIENCE: ("customAudience", "customAudience"),
    SignalKind.USER_LIST: ("userList", "userList"),
}


def audience_dimensions(result: AudienceTargetingResult) -> list[dict]:
    """AND across dimensions, OR within a segment dimension - so one segment dimension holds
    every positive. Splitting them intersects the groups, narrowing rather than widening."""
    by_ref = {s.ref: s for s in result.signals}
    dimensions: list[dict] = []

    for group in result.dimension_groups:
        segments = []
        for ref in group:
            signal = by_ref.get(ref)
            if signal is None:
                continue
            outer, inner = _SEGMENT_FIELD[signal.kind]
            segments.append({outer: {inner: ref}})
        if segments:
            dimensions.append({"audienceSegments": {"segments": segments}})

    demo = result.demographics
    if demo.age_ranges:
        dimensions.append(
            {
                "age": {
                    # maxAge is omitted entirely for an open-ended band - sending null is not
                    # the same as leaving it unset. Ints, not the AgeRangeType enum: the enum
                    # returns "Invalid value ... (TYPE_INT32)".
                    "ageRanges": [
                        {
                            "minAge": r.min_age,
                            **({"maxAge": r.max_age} if r.max_age else {}),
                        }
                        for r in demo.age_ranges
                    ],
                    "includeUndetermined": demo.includes_undetermined("age_ranges"),
                }
            }
        )
    if demo.genders:
        dimensions.append(
            {
                "gender": {
                    "genders": [g.value for g in demo.genders],
                    "includeUndetermined": demo.includes_undetermined("genders"),
                }
            }
        )
    if demo.income_ranges:
        dimensions.append(
            {
                "householdIncome": {
                    "incomeRanges": [i.value for i in demo.income_ranges],
                    "includeUndetermined": demo.includes_undetermined("income_ranges"),
                }
            }
        )
    if demo.parental_statuses:
        dimensions.append(
            {
                "parentalStatus": {
                    "parentalStatuses": [p.value for p in demo.parental_statuses],
                    "includeUndetermined": demo.includes_undetermined(
                        "parental_statuses"
                    ),
                }
            }
        )
    return dimensions


def audience_exclusions(result: AudienceTargetingResult) -> dict | None:
    """ExclusionSegment has one variant, user_list - nothing else is excludable."""
    lists = [
        {"userList": {"userList": s.ref}}
        for s in result.signals
        if s.negative and s.kind is SignalKind.USER_LIST
    ]
    return {"exclusions": lists} if lists else None


def operations(
    *,
    customer_id: str,
    campaign_name: str,
    budget_micros: int,
    build: dict,
    product_name: str = "",
    start_date: str = "",
    end_date: str = "",
    surfaces: dict[str, bool] | None = None,
    geo_targets: list[str] | None = None,
) -> list[dict]:
    """Every operation for one Demand Gen campaign, in dependency order.

    ``build`` is the channel's block from ``campaign_build`` - this reads its ``audience``
    slot and will read ``channel_controls`` and ``creative`` as they land.
    """
    audience_dump = build.get("audience") or {}
    if not audience_dump:
        raise ValueError("a Demand Gen campaign needs an audience")
    if not geo_targets:
        # Google reads no location criteria as "everywhere", so this cannot be a warning.
        raise ValueError("a Demand Gen campaign needs at least one location to target")
    pending = [
        s["ref"] for s in audience_dump.get("signals") or [] if is_pending(s["ref"])
    ]
    if pending:
        # publish materialises blueprints before calling this; reaching here means it did not.
        raise ValueError(f"custom segment was never created: {pending[0]}")

    result = AudienceTargetingResult.model_validate(audience_dump)
    dimensions = audience_dimensions(result)
    if not any("audienceSegments" in d for d in dimensions):
        # Demographics alone is not a narrow campaign, it is everyone in the country who
        # happens to match. Same rule apply_edit enforces on deleting the last positive.
        raise ValueError("a Demand Gen ad group needs at least one audience segment")

    ad_type = ad_type_for(build.get("creative"))
    temp = TempIds(customer_id)
    budget_rn = temp.take("campaignBudgets")
    campaign_rn = temp.take("campaigns")
    audience_rn = temp.take("audiences")
    ad_group_rn = temp.take("adGroups")

    audience: dict[str, Any] = {
        "resourceName": audience_rn,
        "name": campaign_name,
        "description": f"adzump:v1:product={product_name}",
        "dimensions": dimensions,
    }
    if exclusions := audience_exclusions(result):
        audience["exclusionDimension"] = exclusions

    return [
        budget_operation(
            resource_name=budget_rn,
            name=f"{campaign_name} budget",
            amount_micros=budget_micros,
        ),
        # No network_settings and no advertisingChannelSubType: both are Search/Display
        # concepts, and Demand Gen selects surfaces on the ad group instead.
        campaign_operation(
            resource_name=campaign_rn,
            name=campaign_name,
            channel_type=CHANNEL_TYPE,
            budget_resource_name=budget_rn,
            bidding=BIDDING,
            start_date_time=as_campaign_datetime(start_date),
            end_date_time=as_campaign_datetime(end_date, end_of_day=True),
        ),
        {"audienceOperation": {"create": audience}},
        {
            "adGroupOperation": {
                "create": {
                    "resourceName": ad_group_rn,
                    "name": campaign_name,
                    "status": "ENABLED",
                    "campaign": campaign_rn,
                    # IMMUTABLE and mandatory for Demand Gen - getting it wrong means
                    # deleting and rebuilding the ad group with its ads and criteria.
                    # A sibling of demandGenAdGroupSettings, not nested inside it.
                    "audienceSetting": {"useAudienceGrouped": True},
                    "demandGenAdGroupSettings": {
                        # oneof: selectedChannels OR channelStrategy, never both. normalize
                        # fills the ad type's defaults and forces off what it cannot serve,
                        # so an unset slot still emits correctly.
                        "channelControls": {
                            "selectedChannels": normalize_controls(
                                surfaces or build.get("channel_controls"), ad_type
                            )
                        }
                    },
                }
            }
        },
        {
            "adGroupCriterionOperation": {
                "create": {
                    "adGroup": ad_group_rn,
                    "audience": {"audience": audience_rn},
                }
            }
        },
        # Demand Gen puts location on the AD GROUP, the opposite of Search's campaign-level
        # CampaignCriterion. Without these the campaign serves worldwide.
        *(
            {
                "adGroupCriterionOperation": {
                    "create": {
                        "adGroup": ad_group_rn,
                        "location": {"geoTargetConstant": geo},
                    }
                }
            }
            for geo in geo_targets or ()
        ),
        *creative_operations(temp=temp, ad_group_rn=ad_group_rn, build=build),
    ]


def creative_operations(*, temp: TempIds, ad_group_rn: str, build: dict) -> list[dict]:
    """Asset creates + the AdGroupAd, converting the platform-neutral creative slot.

    Demand Gen's conversion only: the creative agent is platform-level (Meta needs the same
    copy and images), so each emitter applies its own shape and caps. Empty while the slot is
    unbuilt - the AdGroupAd is what lets a campaign serve, which is why it is created PAUSED.
    """
    creative = build.get("creative")
    if not creative:
        return []
    raise NotImplementedError(
        "creative slot filled but Demand Gen cannot emit it yet - the AdGroupAd and its "
        "asset operations are not built"
    )
