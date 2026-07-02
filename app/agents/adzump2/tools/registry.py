"""Adzump2 tool registry — aggregates all tools for the chat agent.

P0 surface:
- Plan tools (create / read / merge-patch / completeness / validate) over the
  server-side CampaignPlan — the agent's only write path.
- ``present_options`` (reused from the legacy adzump agent) for tagged
  chip questions.
- ``web_fetch`` (reused) for lightweight URL reads during research.
"""

from app.agents.adzump.tools.research import web_fetch
from app.agents.adzump.tools.suggestions import present_options
from app.agents.adzump2.tools.plan import PLAN_TOOLS

ALL_TOOLS = [
    *PLAN_TOOLS,
    present_options,
    web_fetch,
]
