"""The Google campaign-creation tools the CampaignAgent runs.

One tool per channel's build step. Each gates on the channel itself and returns a skip
rather than an error when it does not apply, so the orchestrator offers the whole set and
the campaign's own channel decides which runs.

A module rather than the package ``__init__``: the tool objects share their names with the
modules defining them, and binding both into one namespace shadows the modules.
"""

from __future__ import annotations

from app.agents.adzump.agents.campaign.tools.google.audience_targeting import (
    audience_targeting,
)
from app.agents.adzump.agents.campaign.tools.google.keyword_research import (
    keyword_research,
)

GOOGLE_CAMPAIGN_TOOLS = [keyword_research, audience_targeting]
