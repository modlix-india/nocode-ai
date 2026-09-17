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
    return ToolResult(success=True, data=result)


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

    return ToolResult(
        success=True,
        data=saved,
        summary=f"Recorded the plan for {kind} '{saved['name']}' (now version {saved.get('version')}).",
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

#: Read-only, safe for any agent.
BLUEPRINT_READ_TOOLS: list[ToolDefinition] = [BLUEPRINT_GET, BLUEPRINT_DRIFT]

#: Everything, for agents that build and should record what they built.
BLUEPRINT_TOOLS: list[ToolDefinition] = BLUEPRINT_READ_TOOLS + [BLUEPRINT_SET]
