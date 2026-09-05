"""Deal tools — the `Ticket` entity.

The UI calls it a **Deal** (or opportunity) and gates it on `ROLE_Deal_*`; the
entity and its endpoints are `tickets`. A Ticket belongs to an Owner (a lead)
by `ownerId`, to a Product, and to one stage plus an optional status underneath
that stage.

Two of these tools write, and one of them — `deal_move_stage` — is the most
consequential thing this agent can do. See its docstring.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.leadzump.tools._client import (
    TICKETS,
    and_,
    call_server_function,
    client,
    filt,
    headers,
    humanise,
    label,
    not_found,
    ok,
    or_,
    query_body,
    require_code,
    slim,
    slim_rows,
    to_epoch,
)
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

DEAL_FIELDS = [
    "code",
    "name",
    "phoneNumber",
    "email",
    "productId",
    "stage",
    "status",
    "assignedUserId",
    "source",
    "subSource",
    "tag",
    "dnc",
    "latestComment",
    "expiresOn",
    "createdAt",
    "updatedAt",
]

# What `TicketService.updatableEntity` actually writes back on the generic PUT.
# Everything else in the body is read, discarded, and answered 200. `stage` and
# `status` are reachable here too but deliberately excluded: they belong to
# `deal_move_stage`, which is the route that runs the pipeline's side effects.
DEAL_WRITABLE = {
    "name": "name",
    "description": "description",
    "email": "email",
    "assigned_user_id": "assignedUserId",
    "sub_source": "subSource",
    "tag": "tag",
}


def _deal_condition(params: dict[str, Any]) -> dict | None:
    clauses: list[dict | None] = []

    text = (params.get("q") or "").strip()
    if text:
        clauses.append(
            or_(
                filt("name", text, "STRING_LOOSE_EQUAL"),
                filt("phoneNumber", text, "STRING_LOOSE_EQUAL"),
                filt("email", text, "STRING_LOOSE_EQUAL"),
            )
        )

    for arg, field in (
        ("product_id", "productId"),
        ("stage_id", "stage"),
        ("status_id", "status"),
        ("assigned_user_id", "assignedUserId"),
        ("owner_id", "ownerId"),
    ):
        value = params.get(arg)
        if value not in (None, "", 0):
            clauses.append(filt(field, value))

    for arg, field in (("source", "source"), ("sub_source", "subSource"), ("tag", "tag")):
        value = (params.get(arg) or "").strip()
        if value:
            clauses.append(filt(field, value))

    if params.get("dnc") is not None:
        clauses.append(filt("dnc", None, "IS_TRUE" if params["dnc"] else "IS_FALSE"))

    after = to_epoch(params.get("created_after"))
    if after is not None:
        clauses.append(filt("createdAt", after, "GREATER_THAN_EQUAL"))
    before = to_epoch(params.get("created_before"))
    if before is not None:
        clauses.append(filt("createdAt", before, "LESS_THAN_EQUAL"))

    return and_(*clauses)


async def _deal_search(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    try:
        condition = _deal_condition(params)
    except ValueError as exc:
        return ToolResult(success=False, error=str(exc))

    body = query_body(
        condition,
        page=int(params.get("page") or 0),
        size=int(params.get("size") or 20),
        sort_field=params.get("sort_by") or "updatedAt",
        sort_desc=(params.get("sort") or "desc").lower() != "asc",
    )
    result = await client().post(
        f"{TICKETS}/eager/query", headers=headers(context), json=body
    )
    if not result.success:
        return ToolResult(success=False, error=f"Deal search failed: {result.error}")

    shaped = slim_rows(result.data, DEAL_FIELDS)
    return ok(
        shaped,
        f"{shaped.get('total', 0)} deal(s) matched; showing {len(shaped['rows'])}",
        max_chars=6000,
    )


async def _deal_get(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    code, err = require_code(params)
    if err:
        return err

    result = await client().get(
        f"{TICKETS}/code/{code}/eager",
        headers=headers(context),
        params={"eager": "true"},
    )
    if not result.success:
        return not_found("deal", code) if "404" in (result.error or "") else ToolResult(
            success=False, error=f"Could not read deal {code}: {result.error}"
        )
    if not isinstance(result.data, dict):
        return not_found("deal", code)

    row = humanise(result.data)
    return ok(row, f"deal {row.get('name') or code}", max_chars=8000)


async def _deal_update(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Change a deal's own details.

    Read-modify-write, for the same reason `lead_update` is: `updatableEntity`
    assigns `email`, `assignedUserId`, `subSource` and `tag` from the body with
    no null check, so a partial PUT erases the ones it omits and still answers
    200. Fetching first and sending the whole object back is the only shape
    that means what it says.
    """
    code, err = require_code(params)
    if err:
        return err

    changes = {
        DEAL_WRITABLE[k]: params[k]
        for k in DEAL_WRITABLE
        if k in params and params[k] is not None
    }
    if not changes:
        return ToolResult(
            success=False,
            error=(
                "Nothing to change. Pass at least one of: "
                + ", ".join(sorted(DEAL_WRITABLE))
                + ". To move the deal through the pipeline use deal_move_stage; "
                "product, phone number and source are not editable here."
            ),
        )

    current = await client().get(f"{TICKETS}/code/{code}", headers=headers(context))
    if not current.success or not isinstance(current.data, dict):
        return not_found("deal", code)

    body = dict(current.data)
    body.update(changes)

    result = await client().put(
        f"{TICKETS}/code/{code}", headers=headers(context), json=body
    )
    if not result.success:
        return ToolResult(success=False, error=f"Could not update deal {code}: {result.error}")

    saved = result.data if isinstance(result.data, dict) else {}
    touched = ", ".join(sorted(changes))
    return ok(slim(saved, DEAL_FIELDS), f"deal {saved.get('name') or code}: {touched} updated")


async def _deal_move_stage(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Move a deal to a new pipeline stage (and optionally a status under it).

    Goes through `leadzump.updateStageStatusAndSN`, the same server function
    the kanban and the deal profile call, rather than
    `PATCH /tickets/req/{id}/stage` underneath it. The REST route does every
    platform effect — activity trail, reassignment on a stage change, the new
    stage's messaging rules (which is how a customer gets a WhatsApp
    confirmation), and Meta/Google conversion events for ad-attributed deals —
    but NOT the app's own in-app notification. Taking the shorter path would
    have made an agent-driven move invisible in the notification feed the RM
    actually watches, which is a difference nobody would think to look for.

    So this is not a field edit. It has effects outside the CRM, and the
    confirmation prompt says so before the user approves it.
    """
    code, err = require_code(params)
    if err:
        return err

    stage_id = params.get("stage_id")
    if stage_id in (None, "", 0):
        return ToolResult(
            success=False,
            error=(
                "stage_id is required. Call pipeline_describe for the deal's "
                "product first — a stage from another product template is refused."
            ),
        )

    # Read the deal first: the notification payload names the product, and the
    # function wants the outgoing assignee so it can tell them they lost it.
    current = await client().get(
        f"{TICKETS}/code/{code}/eager", headers=headers(context), params={"eager": "true"}
    )
    if not current.success or not isinstance(current.data, dict):
        return not_found("deal", code)
    deal = current.data

    status_request: dict[str, Any] = {"stageId": stage_id}
    if params.get("status_id") not in (None, "", 0):
        status_request["statusId"] = params["status_id"]
    comment = (params.get("comment") or "").strip()
    if comment:
        status_request["comment"] = comment

    auth = context.get("auth")
    assigned = deal.get("assignedUserId")
    result, error = await call_server_function(
        "leadzump/updateStageStatusAndSN",
        {
            "ticketId": code,
            "ticketStatusRequest": status_request,
            "oldAssignedUser": assigned.get("id") if isinstance(assigned, dict) else assigned,
            "notificationPayLoad": {
                "userName": getattr(auth, "user_name", "") or "the assistant",
                "productName": label(deal.get("productId")) or "",
            },
        },
        context,
    )
    if error:
        return ToolResult(success=False, error=f"Could not move deal {code}: {error}")

    return ok(
        {"code": code, "stageId": stage_id, "statusId": params.get("status_id"),
         "result": result},
        f"deal {deal.get('name') or code} moved to stage {stage_id}",
    )


async def _deal_create(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Create a deal, the way the app does.

    `leadzump.createTicketAndSN` creates the Ticket (and its Owner, if the
    phone number is new) and raises the in-app notification. It also carries
    the duplicate check: a repeat phone number on the same product comes back
    as "A Deal already exists", which is surfaced verbatim so the user can
    decide rather than having a second deal quietly appear.
    """
    name = (params.get("name") or "").strip()
    phone = (params.get("phone_number") or "").strip()
    if not name:
        return ToolResult(success=False, error="name is required.")
    if not phone and not (params.get("email") or "").strip():
        return ToolResult(
            success=False,
            error="A deal needs a phone number or an email — that is how the lead is identified.",
        )
    if params.get("product_id") in (None, "", 0):
        return ToolResult(
            success=False,
            error="product_id is required. Call product_list to see what is on offer.",
        )

    ticket: dict[str, Any] = {"name": name, "productId": params["product_id"]}
    if phone:
        ticket["phoneNumber"] = {
            "countryCode": int(params.get("dial_code") or 91),
            "number": phone,
        }
    if params.get("email"):
        ticket["email"] = {"address": params["email"]}
    if params.get("source"):
        ticket["source"] = params["source"]
    if params.get("sub_source"):
        ticket["subSource"] = params["sub_source"]
    if params.get("description"):
        ticket["description"] = params["description"]
    if params.get("comment"):
        ticket["comment"] = params["comment"]

    auth = context.get("auth")
    result, error = await call_server_function(
        "leadzump/createTicketAndSN",
        {"ticketRequest": ticket, "loggedInUser": getattr(auth, "user_id", None)},
        context,
    )
    if error:
        return ToolResult(success=False, error=f"Could not create the deal: {error}")

    created = result if isinstance(result, dict) else {}
    return ok(
        {"name": name, "productId": params["product_id"], "result": created},
        f"deal '{name}' created",
    )


async def _deal_tag(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Set a deal's tag, with an optional note explaining why."""
    code, err = require_code(params)
    if err:
        return err
    tag = (params.get("tag") or "").strip()
    if not tag:
        return ToolResult(success=False, error="tag is required.")

    body: dict[str, Any] = {"tag": tag}
    if params.get("comment"):
        body["comment"] = params["comment"]

    result = await client().patch(
        f"{TICKETS}/req/{code}/tag", headers=headers(context), json=body
    )
    if not result.success:
        return ToolResult(success=False, error=f"Could not tag deal {code}: {result.error}")

    saved = result.data if isinstance(result.data, dict) else {}
    return ok(slim(saved, DEAL_FIELDS), f"deal {saved.get('name') or code} tagged '{tag}'")


async def _assignee_list(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """The users who currently hold deals the caller can see.

    Backed by the deals-side distinct-assignee query rather than the security
    service's user admin API, so it stays inside this agent's domain and inside
    the caller's own row-level visibility. It is how a model gets the numeric
    `assigned_user_id` that `deal_update` and `deal_search` need.
    """
    try:
        condition = _deal_condition(params)
    except ValueError as exc:
        return ToolResult(success=False, error=str(exc))

    result = await client().post(
        f"{TICKETS}/users/query",
        headers=headers(context),
        json=query_body(condition, size=100, eager=False),
    )
    if not result.success:
        return ToolResult(success=False, error=f"Could not list assignees: {result.error}")

    users = result.data if isinstance(result.data, list) else []
    rows = [
        {
            "id": u.get("id"),
            "name": " ".join(
                p for p in [(u.get("firstName") or ""), (u.get("lastName") or "")] if p
            ).strip()
            or u.get("userName"),
            "userName": u.get("userName"),
            "emailId": u.get("emailId"),
        }
        for u in users
        if isinstance(u, dict)
    ]
    return ok({"users": rows[:50], "total": len(rows)}, f"{len(rows)} assignee(s)", max_chars=6000)


# ── tool definitions ────────────────────────────────────────────────────────

_DEAL_FILTERS = [
    ToolParameter(
        name="q",
        type="string",
        description="Free text matched as a substring against the deal's name, phone and email.",
        required=False,
    ),
    ToolParameter(
        name="product_id",
        type="integer",
        description="Numeric product id, from product_list.",
        required=False,
    ),
    ToolParameter(
        name="stage_id",
        type="integer",
        description="Numeric stage id, from pipeline_describe.",
        required=False,
    ),
    ToolParameter(
        name="status_id",
        type="integer",
        description="Numeric status id (a child of a stage), from pipeline_describe.",
        required=False,
    ),
    ToolParameter(
        name="assigned_user_id",
        type="integer",
        description="Numeric user id of the deal's owner, from assignee_list.",
        required=False,
    ),
    ToolParameter(
        name="owner_id",
        type="integer",
        description="Numeric id of the lead (Owner) whose deals to list, from lead_get.",
        required=False,
    ),
    ToolParameter(name="source", type="string", description="Exact lead source.", required=False),
    ToolParameter(
        name="sub_source", type="string", description="Exact lead sub-source.", required=False
    ),
    ToolParameter(name="tag", type="string", description="Exact tag.", required=False),
    ToolParameter(
        name="dnc",
        type="boolean",
        description="True for do-not-contact deals only, false to exclude them.",
        required=False,
    ),
    ToolParameter(
        name="created_after",
        type="string",
        description=(
            "Only deals created at or after this ISO-8601 UTC date/datetime "
            "(e.g. 2026-09-01). All timestamps in this CRM are UTC."
        ),
        required=False,
    ),
    ToolParameter(
        name="created_before",
        type="string",
        description="Only deals created at or before this ISO-8601 UTC date/datetime.",
        required=False,
    ),
]

_DEAL_PAGING = [
    ToolParameter(
        name="sort_by",
        type="string",
        description="Column to sort on. Defaults to updatedAt.",
        required=False,
        enum=["updatedAt", "createdAt", "name", "expiresOn"],
    ),
    ToolParameter(
        name="sort", type="string", description="asc or desc (default desc).", required=False,
        enum=["asc", "desc"],
    ),
    ToolParameter(
        name="page", type="integer", description="Zero-based page number.", required=False, default=0
    ),
    ToolParameter(
        name="size", type="integer", description="Rows per page, 1-100 (default 20).",
        required=False, default=20,
    ),
]

deal_search = ToolDefinition(
    name="deal_search",
    display_name="Search Deals",
    description=(
        "Find deals (the `Ticket` entity — one sales opportunity). Filters "
        "combine with AND; ids for product, stage, status and assignee come "
        "from product_list, pipeline_describe and assignee_list, never from "
        "memory. Results are already scoped to what the signed-in user may see."
    ),
    parameters=[*_DEAL_FILTERS, *_DEAL_PAGING],
    execute=_deal_search,
)

deal_get = ToolDefinition(
    name="deal_get",
    display_name="Get Deal",
    description=(
        "Read one deal in full by its code, with product, stage, status and "
        "assignee resolved to names. Read it before quoting a deal's state or "
        "before moving it."
    ),
    parameters=[
        ToolParameter(
            name="code",
            type="string",
            description="The deal's 22-character code, as returned by deal_search.",
            required=True,
        )
    ],
    execute=_deal_get,
)

deal_update = ToolDefinition(
    name="deal_update",
    display_name="Update Deal",
    kind="elicitation",
    elicit_mode="blocking",
    description=(
        "Change a deal's own details: name, description, email, assignee, "
        "sub-source or tag. Fields you do not pass are left as they are. "
        "Product, phone number and source are fixed at intake and the backend "
        "ignores them here; the pipeline stage belongs to deal_move_stage. "
        "Pauses for the user's confirmation before writing."
    ),
    parameters=[
        ToolParameter(
            name="code", type="string", description="The deal's 22-character code.", required=True
        ),
        ToolParameter(name="name", type="string", description="New deal name.", required=False),
        ToolParameter(
            name="description", type="string", description="New free-text description.", required=False
        ),
        ToolParameter(name="email", type="string", description="New email address.", required=False),
        ToolParameter(
            name="assigned_user_id",
            type="integer",
            description=(
                "Numeric id of the user to assign the deal to, from assignee_list. "
                "Reassigning is logged against the deal."
            ),
            required=False,
        ),
        ToolParameter(name="sub_source", type="string", description="New sub-source.", required=False),
        ToolParameter(name="tag", type="string", description="New tag.", required=False),
    ],
    execute=_deal_update,
)

deal_move_stage = ToolDefinition(
    name="deal_move_stage",
    display_name="Move Deal Stage",
    kind="elicitation",
    elicit_mode="blocking",
    description=(
        "Move a deal to a different pipeline stage, and optionally to a status "
        "under it. This has effects outside the CRM: the new stage's messaging "
        "rules are queued (which can send the customer a WhatsApp message), a "
        "conversion event may be reported to Meta or Google for an ad-attributed "
        "deal, and changing stage re-runs assignment. Read the deal and the "
        "pipeline first, and only move it when the user has asked for that move. "
        "Pauses for the user's confirmation."
    ),
    parameters=[
        ToolParameter(
            name="code", type="string", description="The deal's 22-character code.", required=True
        ),
        ToolParameter(
            name="stage_id",
            type="integer",
            description=(
                "Numeric id of the target stage, from pipeline_describe for THIS "
                "deal's product. A stage from another product template is refused."
            ),
            required=True,
        ),
        ToolParameter(
            name="status_id",
            type="integer",
            description=(
                "Numeric id of a status under that stage. Optional; a status that "
                "is not a child of the stage is ignored rather than refused."
            ),
            required=False,
        ),
        ToolParameter(
            name="comment",
            type="string",
            description="Note recorded on the deal's activity trail with the move.",
            required=False,
        ),
    ],
    execute=_deal_move_stage,
)

assignee_list = ToolDefinition(
    name="assignee_list",
    display_name="List Assignees",
    description=(
        "The users who hold deals the signed-in user can see, with their numeric "
        "ids. Use it to turn a person's name into the `assigned_user_id` that "
        "deal_search and deal_update take. Accepts the same filters as "
        "deal_search to narrow which deals' assignees are listed."
    ),
    parameters=_DEAL_FILTERS,
    execute=_assignee_list,
)

deal_create = ToolDefinition(
    name="deal_create",
    display_name="Create Deal",
    kind="elicitation",
    elicit_mode="blocking",
    description=(
        "Create a new deal for a lead. Needs a name, a product, and a phone "
        "number or email to identify the person by. Goes through the app's own "
        "creation path, so the duplicate check runs and the assignee is "
        "notified. If a deal already exists for that number on that product "
        "the backend refuses and says so — do not retry with a changed number "
        "to get around it. Pauses for the user's confirmation."
    ),
    parameters=[
        ToolParameter(name="name", type="string", description="The deal name, usually the person's name.", required=True),
        ToolParameter(
            name="product_id",
            type="integer",
            description="Numeric product id, from product_list.",
            required=True,
        ),
        ToolParameter(
            name="phone_number",
            type="string",
            description="Digits only, without the country code.",
            required=False,
        ),
        ToolParameter(
            name="dial_code",
            type="integer",
            description="Country calling code as a number, e.g. 91. Defaults to 91.",
            required=False,
            default=91,
        ),
        ToolParameter(name="email", type="string", description="Email address.", required=False),
        ToolParameter(
            name="source",
            type="string",
            description="Lead source — read source_list first, it is matched exactly.",
            required=False,
        ),
        ToolParameter(name="sub_source", type="string", description="Lead sub-source.", required=False),
        ToolParameter(name="description", type="string", description="Free-text description.", required=False),
        ToolParameter(name="comment", type="string", description="Opening note on the deal.", required=False),
    ],
    execute=_deal_create,
)

deal_tag = ToolDefinition(
    name="deal_tag",
    display_name="Tag Deal",
    kind="elicitation",
    elicit_mode="blocking",
    description=(
        "Set a deal's tag, optionally with a note saying why. Pauses for the "
        "user's confirmation."
    ),
    parameters=[
        ToolParameter(name="code", type="string", description="The deal's 22-character code.", required=True),
        ToolParameter(name="tag", type="string", description="The tag to set.", required=True),
        ToolParameter(name="comment", type="string", description="Why, recorded with the change.", required=False),
    ],
    execute=_deal_tag,
)

DEAL_TOOLS = [
    deal_search,
    deal_get,
    deal_create,
    deal_update,
    deal_move_stage,
    deal_tag,
    assignee_list,
]
