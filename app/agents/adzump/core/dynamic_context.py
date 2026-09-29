"""Dynamic context: the per-turn reminder for any adzump agent.

    DynamicContext   what one agent declares: its journey + its own fixed text
    render()         builds the whole reminder before every model call

The reminder is rebuilt every time and never kept in chat history. It always
has this shape:

    <steers>                  the agent's one-turn notes, if any
    ## State                  from the journey walk: one line per step
    ## User just said         the latest user message, word for word
    <reply rules>             the agent's own "How to respond"
    ## What's still missing   from the journey walk: next action first

The orchestrator's declaration is ``workflow.ORCHESTRATOR_CONTEXT``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Generic

from app.agents.adzump.core.journey import C, Journey, Progress


# What one agent declares once:
#   journey       its steps (core/journey.py)
#   reply_rules   its fixed "How to respond" section
#   missing_note  its line above the numbered missing list (optional)
@dataclass(frozen=True)
class DynamicContext(Generic[C]):
    journey: Journey[C]
    reply_rules: str
    missing_note: str = ""

    # Called before every model call. Walks the journey over the agent's
    # snapshot (`ctx`) and puts the sections together in the order above;
    # empty ones are dropped. Returns the text and the walk it came from (the
    # caller logs what was owed).
    #   last_user     the user's latest message
    #   agentic_turn  which model call within this one reply (1 = the first)
    #   set_at        field -> the turn it was saved, for "set N turns ago"
    #   session_turn  which user message we're on (the "now" for set_at)
    #   steers        the agent's one-turn notes, placed on top
    def render(
        self,
        ctx: C,
        *,
        last_user: str,
        agentic_turn: int,
        set_at: Mapping[str, int] | None = None,
        session_turn: int = 0,
        steers: Sequence[str | None] = (),
    ) -> tuple[str, Progress]:
        progress = self.journey.walk(ctx, set_at, session_turn)
        sections = [
            *steers,
            progress.state_section(),
            _user_said_section(last_user, agentic_turn),
            self.reply_rules,
            progress.missing_section(self.missing_note),
        ]
        return "\n".join(section for section in sections if section), progress


# The user's latest message, fenced so the model reads it word for word. From
# the second model call of the same reply on, it says the message was already
# acknowledged, so the model doesn't respond to it twice.
def _user_said_section(last_user: str, agentic_turn: int = 1) -> str:
    if not last_user:
        return "\n## User just said\n(no user message yet)"
    # Keep the user's line structure - flattening "1. X\n2. Y\n3. Z" into one
    # line turned a typed competitor LIST into a paragraph the model half-read
    # (live 2026-09-09). Fenced so the model sees it verbatim.
    preview = last_user.strip()
    if len(preview) > 500:
        preview = preview[:500] + "…"
    if agentic_turn > 1:
        # Every step of one reply sees this same message; a later step read it
        # as new and acknowledged it again (live 2026-09-25: "Meta it is..."
        # twice around the targeting run).
        return ("\n## User's message (already acknowledged earlier in this reply - "
                f"carry on with the next action, don't acknowledge it again)\n'''\n{preview}\n'''")
    return f"\n## User just said\n'''\n{preview}\n'''"
