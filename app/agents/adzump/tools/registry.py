"""Adzump tool registry — aggregates all tools for the chat agent.

Tools are organized by domain:
- Business analysis (scrape, product profiling)
- Competitor analysis (discovery, verification, craft rendering)
- Account management (Google Ads, Meta)
- Campaign data (stores campaign fields in session context)
- Suggestions (present clickable options to the user)
- Research (web search, fetch)
"""

from app.agents.adzump.tools.product import BUSINESS_TOOLS
from app.agents.adzump.tools.competitor import COMPETITOR_TOOLS
from app.agents.adzump.tools.accounts import ACCOUNT_TOOLS
from app.agents.adzump.tools.campaign_data import CAMPAIGN_DATA_TOOLS
from app.agents.adzump.tools.suggestions import SUGGESTION_TOOLS
from app.agents.adzump.tools.research import RESEARCH_TOOLS

ALL_TOOLS = [
    *BUSINESS_TOOLS,
    *COMPETITOR_TOOLS,
    *ACCOUNT_TOOLS,
    *CAMPAIGN_DATA_TOOLS,
    *SUGGESTION_TOOLS,
    *RESEARCH_TOOLS,
]
