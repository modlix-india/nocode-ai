"""LeadZump tool registry.

Twenty-one tools over the CRM's own records: thirteen read, eight write.
Two of the reads exist purely so the others can be given real values rather
than guessed ones: `assignee_list` for user ids and
`source_list` for the source taxonomy, which is matched exactly.

Everything routes through the gateway with the caller's own token, so the
entity-processor applies both tenancy and the caller's row-level visibility on
every call. No tool takes a client code.
"""

from app.agents.leadzump.tools.catalog import CATALOG_TOOLS
from app.agents.leadzump.tools.content import CONTENT_TOOLS
from app.agents.leadzump.tools.deals import DEAL_TOOLS
from app.agents.leadzump.tools.history import HISTORY_TOOLS
from app.agents.leadzump.tools.leads import LEAD_TOOLS

ALL_TOOLS = [
    *LEAD_TOOLS,
    *DEAL_TOOLS,
    *CATALOG_TOOLS,
    *CONTENT_TOOLS,
    *HISTORY_TOOLS,
]

# Every tool that changes stored state. `BaseAgent` pauses each of these for an
# explicit user approval through the SSE `confirmation_request` / `/confirm`
# round trip, and lints that each is declared `kind='elicitation'`.
#
# adzump2 ships this set empty with a TODO; this agent does not, because every
# one of these reaches a real customer record, and two of them reach the
# customer: a stage move queues the stage's messaging rules, and a create
# notifies the assignee.
MUTATING_TOOLS = {
    "lead_update",
    "deal_create",
    "deal_update",
    "deal_move_stage",
    "deal_tag",
    "task_create",
    "task_complete",
    "note_add",
}
