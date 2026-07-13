"""Adzump tool registry - aggregates all tools for the chat agent.

Tools are organized by domain:
- Business analysis (scrape, product profiling)
- Competitor analysis (discovery, verification, craft rendering)
- Account management (Google Ads, Meta)
- Campaign spec (stores campaign-defining fields in session context)
- Suggestions (present clickable options to the user)
- Research (web search, fetch)
"""

from app.agents.adzump.tools.product import BUSINESS_TOOLS
from app.agents.adzump.tools.competitor import COMPETITOR_TOOLS
from app.agents.adzump.tools.accounts import ACCOUNT_TOOLS
from app.agents.adzump.tools.campaign_data import CAMPAIGN_SPEC_TOOLS
from app.agents.adzump.tools.suggestions import SUGGESTION_TOOLS
from app.agents.adzump.tools.research import RESEARCH_TOOLS
from app.agents.adzump.tools.location import LOCATION_TOOLS
from app.agents.adzump.tools.prepare_campaign_review import PREPARE_CAMPAIGN_REVIEW_TOOLS
from app.agents.adzump.tools.launch import LAUNCH_TOOLS
from app.agents.adzump.tools.asset_manage import MANAGE_ASSETS_TOOLS

ALL_TOOLS = [
    *BUSINESS_TOOLS,
    *COMPETITOR_TOOLS,
    *ACCOUNT_TOOLS,
    *CAMPAIGN_SPEC_TOOLS,
    *SUGGESTION_TOOLS,
    *RESEARCH_TOOLS,
    *LOCATION_TOOLS,
    *PREPARE_CAMPAIGN_REVIEW_TOOLS,
    *LAUNCH_TOOLS,
    *MANAGE_ASSETS_TOOLS,
]
