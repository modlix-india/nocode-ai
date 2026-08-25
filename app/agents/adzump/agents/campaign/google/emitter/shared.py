"""What every Google channel's payload needs, regardless of channel type.

The split is evidence-based, not guessed: Google's create-campaign guide and its code samples
set name, advertising_channel_type, status, a bidding field, campaign_budget and
contains_eu_political_advertising on every campaign, and name / delivery_method /
amount_micros on every budget. Which bidding field, and network_settings, are channel
business - Demand Gen must not send network_settings at all.

https://developers.google.com/google-ads/api/docs/campaigns/create-campaigns
"""

from __future__ import annotations

from typing import Any

MICROS = 1_000_000

# Required on create in practice: omitting it returns fieldError REQUIRED, verified live.
NO_EU_POLITICAL = "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"

# "Recommendation: Set the campaign to PAUSED when creating". We also have our own reason -
# a campaign with no ad cannot serve, and an enabled one that cannot serve is worse than an
# obviously unfinished one.
CREATE_STATUS = "PAUSED"


class TempIds:
    """Sequential negative resource ids for one mutate request.

    Fixed constants only work for a channel with a fixed shape. Search creates one ad group
    per keyword theme, so ids must be handed out as it goes - and two channels inventing
    their own schemes would eventually collide inside a shared helper, which surfaces as
    RESOURCE_NOT_FOUND on an unrelated operation.
    """

    def __init__(self, customer_id: str) -> None:
        self._customer_id = customer_id
        self._next = -1

    def take(self, collection: str) -> str:
        name = f"customers/{self._customer_id}/{collection}/{self._next}"
        self._next -= 1
        return name


def budget_operation(
    *, resource_name: str, name: str, amount_micros: int, shared: bool = False
) -> dict:
    """The budget every channel starts with. The per-day minimum is currency- and
    account-dependent, so Google enforces it and we surface its message."""
    return {
        "campaignBudgetOperation": {
            "create": {
                "resourceName": resource_name,
                "name": name,
                "amountMicros": str(amount_micros),
                "deliveryMethod": "STANDARD",
                # Demand Gen budgets cannot be shared; Search's can.
                "explicitlyShared": shared,
            }
        }
    }


def campaign_operation(
    *,
    resource_name: str,
    name: str,
    channel_type: str,
    budget_resource_name: str,
    bidding: dict[str, Any],
    start_date_time: str = "",
    end_date_time: str = "",
    extra: dict[str, Any] | None = None,
) -> dict:
    """The campaign fields common to every channel. ``bidding`` is the channel's strategy
    field; ``extra`` is what only that channel sets (network_settings for Search, nothing for
    Demand Gen).

    biddingStrategyType is never sent - OUTPUT_ONLY, and accepted silently if you do.
    """
    campaign: dict[str, Any] = {
        "resourceName": resource_name,
        "name": name,
        "status": CREATE_STATUS,
        "advertisingChannelType": channel_type,
        "campaignBudget": budget_resource_name,
        "containsEuPoliticalAdvertising": NO_EU_POLITICAL,
        **bidding,
    }
    if start_date_time:
        campaign["startDateTime"] = start_date_time
    if end_date_time:
        campaign["endDateTime"] = end_date_time
    campaign.update(extra or {})
    return {"campaignOperation": {"create": campaign}}


def as_campaign_datetime(date: str, *, end_of_day: bool = False) -> str:
    """A yyyy-MM-dd date as "yyyy-MM-dd HH:mm:ss" in the customer's time zone. A bare date is
    rejected, so the time component is not optional.

    Start takes midnight, end takes the last second: an END of 00:00:00 fails with
    DATE_RANGE_ERROR_END_TIME_MUST_BE_THE_END_OF_A_DAY. The proto documents only the first.
    """
    date = date.strip()
    if not date or " " in date:
        return date
    return f"{date} 23:59:59" if end_of_day else f"{date} 00:00:00"
