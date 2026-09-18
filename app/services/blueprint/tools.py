"""The blueprint as agent tools. Three verbs, and three is the whole point.

  blueprint_get    what is this object meant to be
  blueprint_set    record what it is meant to be
  blueprint_drift  where does the plan disagree with what was built

**Applying a plan is not a tool.** "Build the hero section this plan describes"
is the agent doing its ordinary work — `add_component`, `set_styles`,
`set_bindings` — guided by what it read. Wrapping that in a `blueprint_apply`
tool would either duplicate the page tools or hide them behind a worse
interface, and the agent already has sixty tools competing for its attention.

Reads are cheap and the agent is encouraged to call `blueprint_get` before
changing anything: an object's plan is the only record of why it is the way it
is, and an agent that does not know it exists will cheerfully undo a decision.

Writes go through the same validator the editor uses
(`app.services.blueprint.validate`), which is a deliberate second copy of the
TypeScript one. Three writers put JSON into this field and only one of them goes
through the editor.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.services.blueprint import objects
from app.services.blueprint.objects import BlueprintObjectError
from app.services.blueprint.validate import render_issues, validate_blueprint
from app.services.lore import access
from app.services.lore.access import LoreAccessError

logger = logging.getLogger(__name__)


class _Auth:
    """Minimal shape `access.resolve_scope` needs, built from a tool context."""

    def __init__(self, client_code: str) -> None:
        self.client_code = client_code


def _tenant(context: dict[str, Any]) -> tuple[str, str, str | None]:
    """(client_code, app_code, error) from the session context.

    The app comes from the session's focus resolver, the same one lore and the
    write path use, so a session that opened in appbuilder and went on to build
    `crm` plans `crm`. The client code is the logged-in user's and never the app
    owner's.
    """
    auth = context.get("auth")
    headers = context.get("headers") or {}
    client_code = ""
    if auth and getattr(auth, "client_code", None):
        client_code = auth.client_code
    elif headers.get("clientCode"):
        client_code = headers["clientCode"]

    from app.core.session import app_code_from_context
    app_code = app_code_from_context(context)

    if not client_code or not app_code:
        return client_code, app_code, (
            "No app in this session. A blueprint belongs to one application; say "
            "which app to work in before reading or writing a plan."
        )
    return client_code, app_code, None


async def _allowed(context: dict[str, Any], *, for_write: bool) -> tuple[str, str, ToolResult | None]:
    """(client_code, app_code, refusal). An agent inherits its user's access exactly."""
    client_code, app_code, error = _tenant(context)
    if error:
        return "", "", ToolResult(success=False, error=error)
    try:
        scope = await access.resolve_scope(_Auth(client_code), app_code)
        if for_write:
            scope.require_write()
        else:
            scope.require_read()
    except LoreAccessError as exc:
        return "", "", ToolResult(success=False, error=exc.message)
    return client_code, app_code, None


def _headers(context: dict[str, Any]) -> dict[str, str]:
    return dict(context.get("headers") or {})


# ── Execution ────────────────────────────────────────────────────────────


#: How much of one plan a read may hand back.
#:
#: Well above the 4,000-char default, and the reason is not convenience.
#: `blueprint_set` REPLACES the whole plan, so an agent that has only read part
#: of one cannot safely write it — every key it did not see would be deleted by
#: the write. A truncated read therefore does not degrade this tool, it disables
#: it: the agent correctly refuses to write and says so, which is exactly what
#: happened on a real site whose plan came to about 7,000 characters. Every card
#: on the board was unreachable through the conversation.
#:
#: Still a cap. One read must not eat the context budget, and a plan past this
#: is a plan to read one object at a time rather than one to raise the ceiling
#: for again.
PLAN_READ_CHARS = 60000


async def _get(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    _, app_code, refusal = await _allowed(context, for_write=False)
    if refusal:
        return refusal

    kind = (params.get("kind") or "application").strip()
    name = (params.get("name") or "").strip()
    try:
        result = await objects.read_blueprint(kind, app_code, name, _headers(context))
    except BlueprintObjectError as exc:
        return ToolResult(success=False, error=exc.message)

    if not result["blueprint"]:
        return ToolResult(
            success=True,
            data=result,
            summary=(
                f"{kind} '{result['name']}' has no plan recorded. That is normal for "
                "anything hand-built. Work from the definition, and use blueprint_set "
                "if the user states what it is meant to be."
            ),
        )
    return ToolResult(success=True, data=result, max_result_chars=PLAN_READ_CHARS)


async def _set(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client_code, app_code, refusal = await _allowed(context, for_write=True)
    if refusal:
        return refusal

    blueprint = params.get("blueprint")
    if not isinstance(blueprint, dict):
        return ToolResult(
            success=False,
            error="`blueprint` must be a JSON object: the whole plan for this object.",
        )

    issues = validate_blueprint(blueprint)
    if issues:
        # Refused whole and reported whole. Handing back one problem at a time
        # would cost a round trip per bad key, and a generator with forty of
        # them would never converge.
        return ToolResult(success=False, error=(
            "The plan was refused and nothing was written. Fix every one of these "
            "and call again:\n" + render_issues(issues)
        ))

    kind = (params.get("kind") or "application").strip()
    name = (params.get("name") or "").strip()
    try:
        saved = await objects.write_blueprint(
            kind, app_code, name, blueprint, _headers(context), client_code,
            message=(params.get("message") or "").strip() or "blueprint updated",
        )
    except BlueprintObjectError as exc:
        return ToolResult(success=False, error=exc.message)

    # Keep the app plan's index of this object in step with what just changed.
    #
    # The board reads `pending` per object out of the app plan, because that is
    # the only place it can be read without opening every object: a column's own
    # cards come from that object's document, fetched when somebody opens it. So
    # after the plan agent added two sections to `home`, the app plan still said
    # `home` had nothing outstanding — and the board said "Nothing to build"
    # over two planned sections until the column was opened by hand, at which
    # point Build suddenly had work.
    #
    # Only for a real object. Writing the application's plan re-indexes nothing:
    # it IS the index.
    if kind != "application":
        await _reindex_one(kind, name, app_code, blueprint, _headers(context), client_code)

    # Tell whatever is showing this object that it changed.
    #
    # Announced explicitly rather than left to the HTTP choke point, which
    # cannot see this one: it decides "was that a write?" from the verb and the
    # path, and a plan is saved by PATCHing `/{id}/blueprint` — a sub-path it
    # does not resolve to a page, with a body that is the plan and therefore
    # carries no `name` to report. So the board was never told, and the column
    # somebody was looking at kept showing the plan from before they asked.
    try:
        from app.core.tools.draft_registry import announce_change

        await announce_change(
            kind=kind, obj_id=str(saved.get("id") or ""), name=saved.get("name") or name,
            app_code=app_code, operation="PATCH",
        )
    except Exception:  # noqa: BLE001 — a plan is saved; a refresh hint is not worth failing it
        logger.debug("blueprint: could not announce the write", exc_info=True)

    return ToolResult(
        success=True,
        data=saved,
        summary=f"Recorded the plan for {kind} '{saved['name']}' (now version {saved.get('version')}).",
    )


async def _reindex_one(
    kind: str,
    name: str,
    app_code: str,
    blueprint: dict[str, Any],
    headers: dict[str, str],
    client_code: str,
) -> None:
    """Refresh one object's row in the app plan's index, and nothing else.

    Recomputed from the plan just written rather than re-read, so this costs one
    read of the app plan and one write of it — never a sweep. The count is what
    the board acts on, and it must be true the moment the plan changes, not the
    next time somebody runs a sweep.

    `index_objects` is reused rather than reimplemented: it already knows the
    entry shape and, more importantly, already knows what it must NOT touch —
    `purpose` and `spec` are what a person stated and no derived pass may
    overwrite them.

    Failure here is logged and swallowed. The object's plan is saved and correct;
    an index one refresh behind shows a stale count, which is visibly wrong and
    fixable, where failing the write would lose the plan the person just agreed.
    """
    from app.services.blueprint.build_job import pending_sections
    from app.services.blueprint.compose import collection_for, index_objects

    try:
        current = await objects.read_blueprint("application", app_code, "", headers)
        app_plan = current.get("blueprint") or {}
        # The summary is left alone: it is derived by `describe` and is not this
        # write's to invent. An empty one means "keep what is there".
        collection, _ = collection_for(kind)
        seen = [(
            kind, name, "",
            len(pending_sections(blueprint, kind)),
            len(((blueprint.get("plan") or {}).get(collection) or {})),
        )]
        await objects.write_blueprint(
            "application", app_code, "", index_objects(app_plan, seen),
            headers, client_code, message=f"index refreshed for {kind} '{name}'",
        )
    except Exception:  # noqa: BLE001 — the plan is saved; the index can lag
        logger.info(
            "blueprint: could not refresh the app index for %s '%s'", kind, name,
            exc_info=True,
        )


async def _drift(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    _, app_code, refusal = await _allowed(context, for_write=False)
    if refusal:
        return refusal

    kind = (params.get("kind") or "page").strip()
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required.")

    try:
        document = await objects.read_object(kind, app_code, name, _headers(context))
    except BlueprintObjectError as exc:
        return ToolResult(success=False, error=exc.message)

    result = objects.drift_of(document)
    counts = result["counts"]
    if not result["status"] and not result["unplanned"]:
        return ToolResult(
            success=True, data=result,
            summary=f"{kind} '{name}' has no plan entries to compare.",
        )
    return ToolResult(success=True, data=result, summary=(
        f"{kind} '{name}': {counts['clean']} agreed, {counts['pending']} planned but "
        f"not built, {counts['drifted']} built but changed since the plan was agreed, "
        f"and {len(result['unplanned'])} built with no plan entry. "
        "Planned-not-built is resolved by BUILDING it; changed-since-agreed is "
        "resolved by UPDATING THE PLAN. They move in opposite directions — never "
        "'sync' one into the other without saying which way."
    ))


# ── Tool definitions ─────────────────────────────────────────────────────
#
# `describe` is NOT among them, though the service has it and the HTTP surface
# exposes it. It is the metered derivation, and the thing that should trigger it
# is a person pressing "Explain this" on a card, not an agent deciding mid-task
# that some context would be nice. An agent can already read the definition it
# is asking about. Three tools against sixty others is a fight for attention
# already; a fourth that spends the customer's tokens on its own initiative is
# the wrong one to add.

BLUEPRINT_GET = ToolDefinition(
    name="blueprint_get",
    display_name="Read the plan for an object",
    description=(
        "What this object is MEANT to be, as opposed to what it is. Read it before "
        "changing anything you did not just create: the plan carries the purpose of "
        "each part and the decisions behind them, and it is the only record of why "
        "something is the way it is. An empty result is normal and means nobody has "
        "planned this object — say so rather than inventing one. "
        "Pass kind='application' for the app's own plan, which lists every object "
        "the app should have and the features they belong to."
    ),
    parameters=[
        ToolParameter(
            name="kind", type="string", required=False, default="application",
            description="Which kind of object.",
            enum=list(objects.KIND_NAMES),
        ),
        ToolParameter(
            name="name", type="string", required=False,
            description="The object's name. Not needed when kind is 'application'.",
        ),
    ],
    execute=_get,
)

BLUEPRINT_SET = ToolDefinition(
    name="blueprint_set",
    display_name="Record the plan for an object",
    description=(
        "Write this object's plan: what it is meant to be and why. Use it when the "
        "user states intent that should outlive the conversation, and after you build "
        "something that was planned, to record what it became.\n\n"
        "This REPLACES the object's whole plan, so call blueprint_get first and send "
        "the merged result. Sending only your new part deletes everything else.\n\n"
        "HARD RULE — no arrays anywhere, at any depth. Every list is an object keyed "
        "by a short letter-first alphanumeric id, each entry carrying an integer "
        "`order` (use gaps of 1000). The platform's override mechanism treats an "
        "array as one opaque value, so a customer who changes one item stops "
        "receiving every later correction to the others, silently. A plan containing "
        "an array is refused whole and nothing is written."
    ),
    parameters=[
        ToolParameter(
            name="blueprint", type="object", required=True,
            description=(
                "The complete plan. Envelope: {schemaVersion, intent, plan, decisions, "
                "origin}. What goes under `plan` depends on the kind — for a page, "
                "{role, layout, route, contentSource, sections}; for a storage, "
                "{entity, grain, fields, relations, lifecycle}; for the application, "
                "{appType, audience, glossary, brand, features, objects}."
            ),
        ),
        ToolParameter(
            name="kind", type="string", required=False, default="application",
            description="Which kind of object.",
            enum=list(objects.KIND_NAMES),
        ),
        ToolParameter(
            name="name", type="string", required=False,
            description="The object's name. Not needed when kind is 'application'.",
        ),
        ToolParameter(
            name="message", type="string", required=False,
            description="One line for the version history: what changed in the plan and why.",
        ),
    ],
    execute=_set,
)

BLUEPRINT_DRIFT = ToolDefinition(
    name="blueprint_drift",
    display_name="Where the plan and the build disagree",
    description=(
        "Compare one object's plan against what is actually built. Three states, and "
        "the two that are not 'clean' point in OPPOSITE directions:\n"
        "  pending  — planned, nothing built for it. Resolve by BUILDING it.\n"
        "  drifted  — built, then changed by hand since the plan was agreed. Resolve "
        "by UPDATING THE PLAN, never by reverting somebody's edit.\n"
        "Also reports parts that exist with no plan entry at all.\n"
        "Never collapse these into one 'out of sync': doing so is how an update ends "
        "up overwriting the work it was meant to record."
    ),
    parameters=[
        ToolParameter(
            name="name", type="string", required=True, description="The object's name.",
        ),
        ToolParameter(
            name="kind", type="string", required=False, default="page",
            description="Which kind of object.",
            enum=list(objects.KIND_NAMES),
        ),
    ],
    execute=_drift,
)

async def _relations(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """What reaches one object, and what it reaches.

    Reads the graph off the application's plan rather than the object's own,
    because only the app plan has the INCOMING half: no object can compute what
    reaches it, and "what reaches it" is the direction that answers the
    questions people actually ask.
    """
    _, app_code, refusal = await _allowed(context, for_write=False)
    if refusal:
        return refusal

    try:
        app_plan = await objects.read_blueprint("application", app_code, "", _headers(context))
    except BlueprintObjectError as exc:
        return ToolResult(success=False, error=exc.message)

    plan = (app_plan.get("blueprint") or {}).get("plan") or {}
    graph = plan.get("relations")
    if not isinstance(graph, dict) or not graph:
        return ToolResult(success=True, data={"relations": {}}, summary=(
            "No connections have been mapped for this app yet. They are derived "
            "from the definitions during a sweep, so run one — or say plainly "
            "that the connections are not known rather than guessing at them."
        ))

    name = (params.get("name") or "").strip()
    kind = (params.get("kind") or "").strip()
    if not name:
        return ToolResult(success=True, data={"relations": graph}, summary=(
            f"{len(graph)} connections across the app."
        ), max_result_chars=PLAN_READ_CHARS)

    address = f"{kind}:{name}" if kind else name
    reaches = {
        uid: entry for uid, entry in graph.items()
        if isinstance(entry, dict) and _matches(entry.get("from"), address, name)
    }
    reached_by = {
        uid: entry for uid, entry in graph.items()
        if isinstance(entry, dict) and _matches(entry.get("to"), address, name)
    }
    if not reaches and not reached_by:
        return ToolResult(success=True, data={"reaches": {}, "reachedBy": {}}, summary=(
            f"Nothing connects '{name}' to anything else. For a page that means "
            "nothing links to it and it reads no data of its own — worth saying "
            "out loud, because an unreachable page is usually a mistake."
        ))

    return ToolResult(success=True, data={"reaches": reaches, "reachedBy": reached_by}, summary=(
        f"'{name}' reaches {len(reaches)} things and is reached by "
        f"{len(reached_by)}. Anything that REACHES it depends on it: removing or "
        "renaming it breaks each one, so say which before proposing either."
    ), max_result_chars=PLAN_READ_CHARS)


def _matches(address: Any, qualified: str, bare: str) -> bool:
    """A `kind:name` endpoint, matched with or without its kind.

    A caller that knows the kind gets an exact match; one that gives a name
    alone still gets an answer, because a person asking about "orderRequest"
    should not have to know it is a storage.
    """
    if not isinstance(address, str):
        return False
    return address == qualified or address.split(":", 1)[-1] == bare


BLUEPRINT_RELATIONS = ToolDefinition(
    name="blueprint_relations",
    display_name="What connects to what",
    description=(
        "What one object reaches, and what reaches it. Read this BEFORE proposing "
        "to remove, rename, split or replace anything — the things that reach it "
        "are the things that break, and they are not visible from the object "
        "itself or from looking at the site.\n\n"
        "Each connection says how: reads, writes to, deletes from, runs, goes to, "
        "answers with, links to. Those are different relationships and the "
        "difference decides the answer — a page that READS a storage survives it "
        "being emptied, one that DELETES from it is the reason it empties.\n\n"
        "CALL THIS ONCE. With no name it returns the app's ENTIRE graph — every "
        "connection between every object — and that one answer contains what a "
        "per-object call would tell you. Asking again for each object in turn is "
        "answering a question you are already holding the answer to, and it is "
        "the single most expensive mistake available here: one real turn spent "
        "thirteen tool calls and eight model round trips on a question the first "
        "call had answered, and the person waited fourteen seconds for it.\n\n"
        "Pass a name ONLY when you want one object's connections and do not need "
        "the rest.\n\n"
        "The connections are derived from the definitions, so they are either "
        "right or absent: an empty answer means nothing in the app names it, not "
        "that nobody looked."
    ),
    parameters=[
        ToolParameter(
            name="name", type="string", required=False,
            description="The object to ask about. Omit for the whole graph.",
        ),
        ToolParameter(
            name="kind", type="string", required=False,
            description=(
                "Which kind, when a name could belong to more than one. Optional: "
                "a name on its own is matched across every kind."
            ),
            enum=list(objects.KIND_NAMES),
        ),
    ],
    execute=_relations,
)


#: Read-only, safe for any agent.
BLUEPRINT_READ_TOOLS: list[ToolDefinition] = [
    BLUEPRINT_GET, BLUEPRINT_DRIFT, BLUEPRINT_RELATIONS,
]

#: Everything, for agents that build and should record what they built.
BLUEPRINT_TOOLS: list[ToolDefinition] = BLUEPRINT_READ_TOOLS + [BLUEPRINT_SET]
