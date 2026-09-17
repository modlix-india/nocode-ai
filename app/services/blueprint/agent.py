"""The conversation that changes a PLAN, and cannot change an app.

── Why this exists at all ───────────────────────────────────────────────

AI Studio's board was wired to the AppBuilder agent, which has sixty-odd tools
and builds things for a living. Asked "can we add a blog?", it did exactly what
it is for: created a storage, wrote two server functions, built two pages, edited
the nav, and then recorded the decision. Every one of those is a correct action
for that agent and the wrong thing to have happened on this screen.

The whole point of a plan is that changing your mind is free and changing the
site is not. A conversation about the plan must be able to say "yes, and here is
what that would take" and write it down — without a single page existing
afterwards. Building is a separate, deliberate act.

── How that is enforced ─────────────────────────────────────────────────

Not by asking nicely in a prompt. This agent is constructed with FOUR tools and
none of them can write a definition: three of them read, and the one that writes
writes a `blueprint` field. There is no create_page here to be talked into. A
prompt that says "do not build" in front of a toolbox that builds is a
suggestion; a toolbox with no hammer in it is a guarantee.

The trade is that this agent cannot answer "what does the home page actually
look like" in detail — it sees names, titles and plans, not component trees. That
is the right side of the trade for a screen whose subject is intent.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.agent import BaseAgent
from app.core.context import BaseContext
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.services.blueprint import objects
from app.services.blueprint.objects import BlueprintObjectError
from app.services.blueprint.tools import (
    BLUEPRINT_DRIFT,
    BLUEPRINT_GET,
    BLUEPRINT_SET,
    _allowed,
    _headers,
)

logger = logging.getLogger(__name__)


PLAN_PERSONA = """You are the planning half of Modlix. You work on an app's
BLUEPRINT: the record of what it is meant to be, why, and what has deliberately
been left out.

WHAT YOU CAN DO
You can read any object's plan, see where a plan and the built app disagree, and
write a plan. That is the whole of it. You have no tool that creates a page, a
storage, a function or a component, and no tool that edits one. Nothing you do
puts anything on the customer's site.

THE ONE THING TO BE CLEAR ABOUT
When somebody asks for something new — "can we add a blog?" — they are asking to
change the plan. Answer it as a planning question:

  1. Say whether the plan already made a call on it, and quote it if so. A plan
     that deliberately excluded something is a decision, and reversing a decision
     is a thing to do on purpose rather than by accident.
  2. Say what it would take, in the app's own terms: which objects, which pages,
     which fields, what has to be authored by a person afterwards.
  3. If they want it, WRITE IT INTO THE PLAN — the new objects as planned
     entries, and the reversal recorded so the plan does not go on contradicting
     itself. Then say plainly that nothing is built yet and that building is the
     next, separate step.

Never imply you have built something. Never say "I have created" about anything.
"Added to the plan" is the truthful phrase and it is also the useful one.

HOW TO WRITE A PLAN
`blueprint_set` validates before it saves and will refuse what it cannot store.
The rules that matter:
  - NO ARRAYS anywhere, at any depth. A list is a map keyed by a short uid, with
    an integer `order` on each entry, spaced by 1000.
  - A uid starts with a letter: [A-Za-z][A-Za-z0-9]*.
  - `describes` is DERIVED — what a thing is, read off the definition.
    `purpose` is STATED by a person — what it is FOR. Never overwrite a
    `purpose` you did not just hear from the person you are talking to.
  - Read the object's current plan with `blueprint_get` before writing it, and
    send the whole plan back. A write replaces what is there.

STYLE
Short. The person is reading a board while you talk. Name real objects rather
than describing them, and when you have written something, say in one line what
changed and what it would take to build."""


async def _list_objects(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """What the app HAS, by name. The grounding this agent gets instead of tools."""
    # (client_code, app_code, refusal) — in that order. Reading it as
    # (app_code, ...) asked the security service whether this user may read an
    # app named after their own client, which it answered "no" to, correctly.
    _, app_code, refusal = await _allowed(context, for_write=False)
    if refusal:
        return refusal
    kinds = [k.strip() for k in str(params.get("kinds") or "page,storage").split(",") if k.strip()]
    headers = _headers(context)
    found: dict[str, Any] = {}
    for kind in kinds:
        try:
            rows = await objects.list_objects(kind, app_code, headers)
        except BlueprintObjectError as exc:
            found[f"{kind}Error"] = exc.message
            continue
        found[f"{kind}s"] = {
            r["name"]: {"title": r.get("title") or "", "description": r.get("description") or ""}
            for r in rows
        }
    return ToolResult(success=True, data=found)


BLUEPRINT_OBJECTS = ToolDefinition(
    name="blueprint_objects",
    display_name="List what the app has",
    description=(
        "What the app already HAS, by kind: every page and storage with its name "
        "and title. Read this before answering a question about what the app is, or "
        "about what adding something would touch. The plan describes intent; this is "
        "what exists."
    ),
    parameters=[
        ToolParameter(
            name="kinds", type="string", required=False, default="page,storage",
            description="Comma separated kinds to list.",
        ),
    ],
    execute=_list_objects,
)


PLAN_TOOLS: list[ToolDefinition] = [
    BLUEPRINT_OBJECTS,
    BLUEPRINT_GET,
    BLUEPRINT_DRIFT,
    BLUEPRINT_SET,
]


class PlanAgent(BaseAgent):
    """Reads and writes plans. Builds nothing."""

    # Nothing here pauses for confirmation. The only write goes to a `blueprint`
    # field, which is versioned, overridable and reversible — and asking a person
    # to confirm a change to a document they are watching change is noise.
    CONFIRMATION_TOOLS: set[str] = set()

    def __init__(self, model_tier: str = "balanced") -> None:
        super().__init__(
            name="blueprint",
            tools=PLAN_TOOLS,
            context_builder=BaseContext(static_prefix=PLAN_PERSONA),
            model_tier=model_tier,
            # Low on purpose. A planning turn is read, think, write — anything
            # spending thirty turns here is looping, not planning.
            max_turns=12,
            max_tokens=8000,
        )

    display_name = "Plan"
