"""Campaign build -> the atomic googleAds:mutate payload, one builder per channel.

A channel's payload is not a variation of another's. Search puts location and language on the
campaign, creates one ad group per keyword theme and targets N keyword criteria; Demand Gen
puts surfaces on the ad group and targets exactly one audience. So each channel gets its own
module, and ``shared`` holds only what Google requires of every campaign.

The registry is the channel gate: the publish tool looks a channel up here and reports
honestly when there is no builder, instead of growing an if-chain.

Adding a channel: write ``<channel>.py`` with an ``operations(...)``, add one row below, and
validateOnly the CODE-GENERATED payload against a client account before trusting it - that is
how the v23/v24 `maps` mismatch was caught, which no proto would have shown.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.agents.adzump.agents.campaign.google.emitter import demand_gen
from app.agents.adzump.agents.campaign.google.emitter.shared import (
    MICROS,
    TempIds,
    as_campaign_datetime,
    budget_operation,
    campaign_operation,
    parse_mutate_errors,
)
from app.agents.adzump.agents.campaign.models import Channel

OPERATIONS: dict[Channel, Callable[..., list[dict[str, Any]]]] = {
    Channel.DEMAND_GEN: demand_gen.operations,
    # Channel.SEARCH: search.operations,  -- not built; the publish tool reports it as such
}

__all__ = [
    "MICROS",
    "OPERATIONS",
    "TempIds",
    "as_campaign_datetime",
    "budget_operation",
    "campaign_operation",
    "parse_mutate_errors",
]
