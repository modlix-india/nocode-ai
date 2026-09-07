"""LeadZump tool registry.

Forty-one tools in four bands, because "help me with my CRM" turned out to
mean four different jobs:

* **The pipeline** (`leads`, `deals`, `catalog`, `content`, `history`) — the
  records a relationship manager works all day. Written here, over
  entity-processor.
* **CRM configuration** (`config`) — products, pipeline stages, sources, task
  types. Also entity-processor, and nothing else in the codebase covers it.
* **Team and org** (`org`) — users, profiles, roles, departments. NOT written
  here: reused from the AppBuilder agent, which already wraps the security
  service. See `org.py` for which thirteen of its thirty were taken and why.
* **App authoring** — pages, themes, styles. Deliberately absent. AppBuilder
  does that well and it is a different product from a CRM assistant; the
  decision is recorded rather than assumed.

Everything routes through the gateway with the caller's own token, so the
backend applies whatever it enforces on each surface. No tool takes a client
code. Note the surfaces differ sharply in how much that is: the security
service checks authorities on essentially every route, entity-processor checks
them almost nowhere — `org.py` has the numbers.
"""

from app.agents.leadzump.tools.catalog import CATALOG_TOOLS
from app.agents.leadzump.tools.config import CONFIG_MUTATING, CONFIG_TOOLS
from app.agents.leadzump.tools.content import CONTENT_TOOLS
from app.agents.leadzump.tools.deals import DEAL_TOOLS
from app.agents.leadzump.tools.history import HISTORY_TOOLS
from app.agents.leadzump.tools.leads import LEAD_TOOLS
from app.agents.leadzump.tools.org import ORG_MUTATING, ORG_TOOLS

ALL_TOOLS = [
    *LEAD_TOOLS,
    *DEAL_TOOLS,
    *CATALOG_TOOLS,
    *CONTENT_TOOLS,
    *HISTORY_TOOLS,
    *CONFIG_TOOLS,
    *ORG_TOOLS,
]

# Every tool that changes stored state. `BaseAgent` pauses each for an explicit
# user approval through the SSE `confirmation_request` / `/confirm` round trip,
# and lints that each declares `kind='elicitation'`.
#
# adzump2 ships this set empty with a TODO; this agent does not. Each of these
# reaches a real record, and several reach a real person: a stage move queues
# the stage's messaging rules, a deal create notifies the assignee, and
# `make_user_inactive` stops someone signing in.
_PIPELINE_MUTATING = {
    "lead_update",
    "deal_create",
    "deal_update",
    "deal_move_stage",
    "deal_tag",
    "task_create",
    "task_complete",
    "note_add",
}

MUTATING_TOOLS = _PIPELINE_MUTATING | CONFIG_MUTATING | ORG_MUTATING
