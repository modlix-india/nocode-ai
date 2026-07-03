"""Adzump2 tool registry — aggregates all tools for the chat agent.

P0 surface:
- Plan tools (create / read / merge-patch / completeness / validate) over the
  server-side CampaignPlan — the agent's only write path.
- ``present_options`` (reused from the legacy adzump agent) for tagged
  chip questions.
- ``web_fetch`` (reused) for lightweight URL reads during research.

P1 surface:
- Product-study tools (A2): ``analyze_product`` (study a product before any
  campaign is built) + ``confirm_product_profile`` (write the profile back via
  J9). No campaign is built without a studied product.
- ``draft_plan`` (A3) — the planner/critic/repair generation engine. Drafts a
  complete, valid, high-quality plan (or one section) and edits the plan via the
  same ``update_plan`` write path.
- ``generate_creatives`` (A4) — copy + image briefs + taxonomy attributes +
  lead form for the studied product, gated by a pre-spend critic.
"""

from app.agents.adzump.tools.research import web_fetch
from app.agents.adzump.tools.suggestions import present_options
from app.agents.adzump2.creative.tools import CREATIVE_TOOLS
from app.agents.adzump2.planner.loop import draft_plan
from app.agents.adzump2.product.tools import PRODUCT_STUDY_TOOLS
from app.agents.adzump2.tools.plan import PLAN_TOOLS

ALL_TOOLS = [
    *PLAN_TOOLS,
    *PRODUCT_STUDY_TOOLS,
    draft_plan,
    *CREATIVE_TOOLS,
    present_options,
    web_fetch,
]
