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

import asyncio
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
    BLUEPRINT_RELATIONS,
    BLUEPRINT_SET,
    PLAN_READ_CHARS,
    _allowed,
    _headers,
)

logger = logging.getLogger(__name__)


PLAN_PERSONA = """You are the planning half of Modlix. You work on an app's
BLUEPRINT: the record of what it is meant to be, why, and what has deliberately
been left out.

WHAT YOU CAN DO
You can read any object's plan, see what connects to what, see where a plan and
the built app disagree, and write a plan. That is the whole of it. You have no
tool that creates a page, a storage, a function or a component, and no tool that
edits one. Nothing you do puts anything on the customer's site.

THE CONNECTIONS ARE THE POINT
An app is not a list of objects. It is a graph, and almost every real question
is a question about an edge rather than about a node:

  "can we drop the order form?"      -> what else writes to that storage
  "what breaks if I rename this?"    -> what reaches it
  "where does this data come from?"  -> which function fills it
  "is this page reachable?"          -> does anything link to it

`blueprint_relations` answers all four, and ONE call with no name returns the
whole graph. Read it once, then work from what you are holding. Calling it again
per object is the most expensive habit available on this screen: a single real
turn spent thirteen tool calls and eight model round trips re-asking about
objects the first answer already covered, and the person sat watching for
fourteen seconds.

Call it BEFORE proposing to remove, rename, replace, split or merge anything,
and before saying a change is small. The things that reach an object are the
things that break, and they are not visible from the object itself or from
looking at the site.

Read, decide, write. A planning turn is not a search: if you find yourself
asking a third question before saying anything, you already have enough.

Say what you found in the answer, by name: "three things read `orderRequest` —
the order page, the daily digest and the admin list" is useful, "that may have
dependencies" is not. The connections are derived from the definitions, so an
empty answer means nothing in the app names it, not that nobody looked — and a
page nothing links to is worth saying out loud, because it is usually a mistake
rather than a decision.

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
     itself. Then say plainly that nothing is built yet, and that Build is the
     next, separate step and theirs to press.

A DECISION IS WORTH RECORDING
When a person chooses between real alternatives, or you tell them a site cannot
do something, write it into the application's `decisions` — a SIBLING of `plan`,
not part of it:

  {"<uid>": {"order": 1000, "choice": "what was decided",
             "because": "the reason, in their terms",
             "status": "active" | "rejected" | "superseded",
             "area": "The look", "by": "You, 17 September",
             "supersedes": "<uid of the one it replaces>"}}

`rejected` is for something asked for that a site cannot do — it stays on the
board with the reason, so nobody is told no twice about the same thing.
`superseded` replaces rather than deletes: "we tried that and moved off it" is
the most useful thing anybody can know before proposing it again.

Never imply you have built something. Never say "I have created" about anything.
"Added to the plan" is the truthful phrase and it is also the useful one.

HOW TO WRITE SOMETHING THAT CAN LATER BE BUILT
This is the part that makes the difference between a plan and a note.

A new object goes in the APPLICATION's plan, under `plan.objects`, as one entry:

  {"<uid>": {"order": 5000, "kind": "page", "name": "blogList",
             "purpose": "Somewhere to find the posts",
             "status": "planned",
             "spec": { ... the object's own plan ... }}}

Three fields carry the weight:

  `status: "planned"` is what marks it as work. An entry without it is taken to
  describe something that already exists, so a page you forgot to mark will
  never be built and nothing will say why.

  `spec` is the object's own plan, written in exactly the shape it will take
  once the object exists — `sections` for a page, `fields` for a storage. It
  lives here only because a page nobody has created has no `blueprint` field of
  its own to hold it; building the object MOVES this onto it unchanged. So write
  it properly: what goes in the spec is what gets built.

  Every section in a spec needs a `name`, and a page section needs
  `componentKey: null` — null means "planned, nothing on the site answers to
  this yet", which is true. Never invent a component key.

An entry that is already built keeps its `status: "built"` and has NO `spec`:
its plan is on the object itself, and a second copy here is a second plan that
goes quietly stale.

CHANGING SOMETHING THAT ALREADY EXISTS
This is the common case and it works differently from adding.

Do NOT touch the application plan. Read the OBJECT's own plan with
`blueprint_get`, change the entry for the part being reworked — its `purpose`,
its `spec`, whatever the person actually asked to be different — and write the
object's plan back. Leave its `componentKey` exactly as it is: the section still
exists on the page, and clearing the key would say it does not.

Changing an entry's intent is enough. The board compares the entry against what
was last built for it, so a reworked section shows as "to build" on its own and
Build picks it up. You do not need to mark it, and you must not delete and
re-add it — a new uid loses the history and every note attached to it.

`describes` is not intent. Rewording a description changes nothing and asks for
nothing; if the person wants the section to BE different, that goes in
`purpose`.

To remove something, say so in `purpose` and record a decision. Nothing deletes
a page or a section today, so promising otherwise is a promise the build cannot
keep — say plainly that it needs a person.

A picture somebody asks for is `kind: "asset"` with
`asset: {"use": "favicon", "intent": "what it should show", "assetId": null}`.
It is the one kind that is never a document, so the app plan is the only place
it can live.

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
    wanted = str(params.get("kinds") or ",".join(objects.BOARD_KINDS))
    kinds = [k.strip() for k in wanted.split(",") if k.strip()]
    headers = _headers(context)

    # All nine at once. They are nine independent listings against the same
    # gateway and nothing about one informs another, so doing them in a loop
    # spent nine round trips end to end in the middle of a turn somebody is
    # watching — and this is usually the FIRST thing the agent calls, so the
    # cost lands entirely in the silence before it says anything.
    #
    # The first listing is awaited alone before the rest are gathered. It warms
    # the access check, which is cached per (client, app): firing nine at once
    # into a cold cache had all nine miss and resolve it nine times over.
    async def listing(kind: str) -> tuple[str, Any]:
        try:
            return kind, await objects.list_objects(kind, app_code, headers)
        except BlueprintObjectError as exc:
            return kind, exc

    results: list[tuple[str, Any]] = []
    if kinds:
        results.append(await listing(kinds[0]))
        if len(kinds) > 1:
            results.extend(await asyncio.gather(*(listing(k) for k in kinds[1:])))

    found: dict[str, Any] = {}
    for kind, rows in results:
        if isinstance(rows, BlueprintObjectError):
            found[f"{kind}Error"] = rows.message
            continue
        found[f"{kind}s"] = {
            r["name"]: {"title": r.get("title") or "", "description": r.get("description") or ""}
            for r in rows
        }
    # This is the agent's whole grounding in what the app IS, and it is the one
    # read it makes before answering "what would adding a blog touch". A site
    # with sixty pages runs past the default cap, and a half list is worse than
    # a long one here: the agent proposes creating a page that already exists.
    return ToolResult(success=True, data=found, max_result_chars=PLAN_READ_CHARS)


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
            name="kinds", type="string", required=False,
            description=(
                "Comma separated kinds to list. Defaults to everything an app is "
                "made of — pages, storages, functions, uripaths, templates, "
                "notifications, themes, styles."
            ),
        ),
    ],
    execute=_list_objects,
)


PLAN_TOOLS: list[ToolDefinition] = [
    BLUEPRINT_OBJECTS,
    BLUEPRINT_GET,
    BLUEPRINT_RELATIONS,
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

    async def run(self, user_message, session, event_stream, image_blocks=None,
                  model_override=None):
        """Carry the event stream so a saved plan can announce itself.

        `object_changed` is how the board learns to re-read. It is emitted
        through a per-turn registry, and that registry was installed only by
        `AppBuilderAgent` — so a plan written here changed the object and told
        nobody. `onTurnEnd` re-runs the page's load, which refreshes the app
        plan and the object lists but NOT the per-object plans a column has
        already fetched, so the column somebody was looking at kept showing the
        plan from before they asked, until they reloaded the browser by hand.

        Nothing is declared as held. This agent cannot write a definition, so
        there is no draft to hold: the registry is here purely as the thing
        with the stream on it.
        """
        from app.core.tools.draft_registry import DraftRegistry, open_drafts

        registry = DraftRegistry(session_id=session.session_id)
        registry.stream = event_stream
        token = open_drafts.set(registry)
        try:
            await super().run(
                user_message, session, event_stream, image_blocks, model_override,
            )
        finally:
            open_drafts.reset(token)

    display_name = "Plan"
