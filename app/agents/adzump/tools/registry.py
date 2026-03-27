"""Adzump tool registry — aggregates all tools for the chat agent.

Tools are organized by domain:
- Business analysis (scrape, competitors, locations)
- Keyword research
- Ad creation (copy, assets)
- Account management & publishing
- Optimization
"""

from app.agents.adzump.tools.business import BUSINESS_TOOLS
from app.agents.adzump.tools.accounts import ACCOUNT_TOOLS

ALL_TOOLS = [
    *BUSINESS_TOOLS,
    *ACCOUNT_TOOLS,
]
