"""Lead tools — the `Owner` entity.

The UI, the authorities and every user call this a **Lead**; the entity and its
endpoints are `owners`. That mismatch is load-bearing: `ROLE_Lead_READ` gates
`POST /api/entity/processor/owners/eager/query`, and a tool written against a
`leads/*` route would 404 forever.

An Owner is the person. A Ticket (see `deals.py`) is one opportunity belonging
to them, and one Owner can carry several.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.leadzump.tools._client import (
    OWNERS,
    and_,
    client,
    filt,
    headers,
    humanise,
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

LEAD_FIELDS = [
    "code",
    "name",
    "dialCode",
    "phoneNumber",
    "email",
    "source",
    "subSource",
    "description",
    "createdAt",
    "updatedAt",
]

# What `OwnerService.updatableEntity` actually writes back. Everything else on
# a PUT body is read, ignored, and answered 200 — so a tool that accepted
# `source` here would report a change that never happened. See `_lead_update`.
LEAD_WRITABLE = {
    "name": "name",
    "description": "description",
    "phone_number": "phoneNumber",
    "dial_code": "dialCode",
    "email": "email",
}


def _lead_condition(params: dict[str, Any]) -> dict | None:
    """Build the Owner filter tree from the tool arguments."""
    clauses: list[dict | None] = []

    text = (params.get("q") or "").strip()
    if text:
        # STRING_LOOSE_EQUAL is `LIKE '%value%'` server-side; plain LIKE would
        # need the caller to write the wildcards, which a model gets wrong.
        clauses.append(
            or_(
                filt("name", text, "STRING_LOOSE_EQUAL"),
                filt("phoneNumber", text, "STRING_LOOSE_EQUAL"),
                filt("email", text, "STRING_LOOSE_EQUAL"),
            )
        )

    for arg, field in (("phone", "phoneNumber"), ("email", "email")):
        value = (params.get(arg) or "").strip()
        if value:
            clauses.append(filt(field, value, "STRING_LOOSE_EQUAL"))

    for arg, field in (("source", "source"), ("sub_source", "subSource")):
        value = (params.get(arg) or "").strip()
        if value:
            clauses.append(filt(field, value))

    after = to_epoch(params.get("created_after"))
    if after is not None:
        clauses.append(filt("createdAt", after, "GREATER_THAN_EQUAL"))
    before = to_epoch(params.get("created_before"))
    if before is not None:
        clauses.append(filt("createdAt", before, "LESS_THAN_EQUAL"))

    return and_(*clauses)


async def _lead_search(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    try:
        condition = _lead_condition(params)
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
        f"{OWNERS}/eager/query", headers=headers(context), json=body
    )
    if not result.success:
        return ToolResult(success=False, error=f"Lead search failed: {result.error}")

    shaped = slim_rows(result.data, LEAD_FIELDS)
    return ok(
        shaped,
        f"{shaped.get('total', 0)} lead(s) matched; showing {len(shaped['rows'])}",
        max_chars=6000,
    )


async def _lead_get(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    code, err = require_code(params)
    if err:
        return err

    result = await client().get(
        f"{OWNERS}/code/{code}/eager",
        headers=headers(context),
        params={"eager": "true"},
    )
    if not result.success:
        return not_found("lead", code) if "404" in (result.error or "") else ToolResult(
            success=False, error=f"Could not read lead {code}: {result.error}"
        )
    if not isinstance(result.data, dict):
        return not_found("lead", code)

    row = humanise(result.data)
    return ok(row, f"lead {row.get('name') or code}", max_chars=8000)


async def _lead_update(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Change a lead's own details.

    Read-modify-write, and not as a matter of taste. `updatableEntity` re-reads
    the row and then assigns `dialCode`, `phoneNumber` and `email` from the
    request body **unconditionally** — so a PUT carrying only `name` blanks the
    lead's phone and email and answers 200. The only safe shape is: fetch the
    stored object, change the named fields on it, send the whole thing back.
    """
    code, err = require_code(params)
    if err:
        return err

    changes = {
        LEAD_WRITABLE[k]: params[k]
        for k in LEAD_WRITABLE
        if k in params and params[k] is not None
    }
    if not changes:
        return ToolResult(
            success=False,
            error=(
                "Nothing to change. Pass at least one of: "
                + ", ".join(sorted(LEAD_WRITABLE))
                + ". A lead's source and sub-source are set at intake and are not "
                "editable through this tool."
            ),
        )

    current = await client().get(f"{OWNERS}/code/{code}", headers=headers(context))
    if not current.success or not isinstance(current.data, dict):
        return not_found("lead", code)

    body = dict(current.data)
    body.update(changes)

    result = await client().put(
        f"{OWNERS}/code/{code}", headers=headers(context), json=body
    )
    if not result.success:
        return ToolResult(success=False, error=f"Could not update lead {code}: {result.error}")

    saved = result.data if isinstance(result.data, dict) else {}
    touched = ", ".join(sorted(changes))
    return ok(slim(saved, LEAD_FIELDS), f"lead {saved.get('name') or code}: {touched} updated")


# ── tool definitions ────────────────────────────────────────────────────────

_SEARCH_PARAMS = [
    ToolParameter(
        name="q",
        type="string",
        description=(
            "Free text matched as a substring against the lead's name, phone and "
            "email. Use this when the user names a person rather than a field."
        ),
        required=False,
    ),
    ToolParameter(
        name="phone", type="string", description="Substring of the phone number.", required=False
    ),
    ToolParameter(
        name="email", type="string", description="Substring of the email address.", required=False
    ),
    ToolParameter(
        name="source",
        type="string",
        description="Exact lead source, as configured in the source taxonomy.",
        required=False,
    ),
    ToolParameter(
        name="sub_source", type="string", description="Exact lead sub-source.", required=False
    ),
    ToolParameter(
        name="created_after",
        type="string",
        description=(
            "Only leads created at or after this ISO-8601 UTC date/datetime "
            "(e.g. 2026-09-01 or 2026-09-01T09:00:00Z). All timestamps in this "
            "CRM are UTC."
        ),
        required=False,
    ),
    ToolParameter(
        name="created_before",
        type="string",
        description="Only leads created at or before this ISO-8601 UTC date/datetime.",
        required=False,
    ),
    ToolParameter(
        name="sort_by",
        type="string",
        description="Column to sort on. Defaults to updatedAt.",
        required=False,
        enum=["updatedAt", "createdAt", "name"],
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

lead_search = ToolDefinition(
    name="lead_search",
    display_name="Search Leads",
    description=(
        "Find leads (the `Owner` entity — a person who enquired). Returns a "
        "compact row per match plus the total count. Results are already scoped "
        "to what the signed-in user may see, so an empty result means 'none you "
        "can see', not 'none exist'. Use deal_search for opportunities."
    ),
    parameters=_SEARCH_PARAMS,
    execute=_lead_search,
)

lead_get = ToolDefinition(
    name="lead_get",
    display_name="Get Lead",
    description=(
        "Read one lead in full by its code, with related records resolved to "
        "names. Use it before quoting a lead's details, rather than recalling "
        "them from earlier in the conversation."
    ),
    parameters=[
        ToolParameter(
            name="code",
            type="string",
            description="The lead's 22-character code, as returned by lead_search.",
            required=True,
        )
    ],
    execute=_lead_get,
)

lead_update = ToolDefinition(
    name="lead_update",
    display_name="Update Lead",
    kind="elicitation",
    elicit_mode="blocking",
    description=(
        "Change a lead's own contact details. Only name, description, phone "
        "number, dial code and email can be changed here — source and sub-source "
        "are set at intake and the backend silently ignores them on this route. "
        "Fields you do not pass are left exactly as they are. Pauses for the "
        "user's confirmation before writing."
    ),
    parameters=[
        ToolParameter(
            name="code", type="string", description="The lead's 22-character code.", required=True
        ),
        ToolParameter(name="name", type="string", description="New display name.", required=False),
        ToolParameter(
            name="description", type="string", description="New free-text description.", required=False
        ),
        ToolParameter(
            name="phone_number",
            type="string",
            description="New phone number, digits only, without the country code.",
            required=False,
        ),
        ToolParameter(
            name="dial_code",
            type="integer",
            description="New country calling code as a number, e.g. 91.",
            required=False,
        ),
        ToolParameter(name="email", type="string", description="New email address.", required=False),
    ],
    execute=_lead_update,
)

LEAD_TOOLS = [lead_search, lead_get, lead_update]
