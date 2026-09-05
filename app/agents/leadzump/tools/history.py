"""What has already happened on a record: activity, notes, notifications.

`deal_get` answers *what a deal is*. These answer *what has been done about
it*, which is most of what a manager asks and all of what an RM needs before
they ring someone. Without them the agent can describe a deal's current stage
and nothing about how it got there.

All three are reads, and all three inherit the caller's scoping: activity and
notes come from entity-processor under the caller's tenant, notifications from
the notification service for the caller's own user.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.leadzump.tools._client import (
    ACTIVITIES,
    NOTES,
    NOTIFICATIONS,
    client,
    filt,
    from_epoch,
    headers,
    ok,
    query_body,
    require_code,
    slim_rows,
)
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

ACTIVITY_FIELDS = [
    "activityAction",
    "comment",
    "activityDate",
    "actorId",
    "stageId",
    "statusId",
    "createdAt",
]

NOTE_FIELDS = ["code", "name", "content", "createdBy", "createdAt", "updatedAt"]


async def _deal_activity(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """The deal's timeline — stage moves, assignments, notes and tasks logged.

    `Identity` in the path takes a 22-character code directly, so the same
    code every other deal tool uses works here without a lookup.
    """
    code, err = require_code(params)
    if err:
        return err

    size = max(1, min(int(params.get("size") or 20), 50))
    result = await client().get(
        f"{ACTIVITIES}/tickets/{code}/eager",
        headers=headers(context),
        params={"eager": "true", "page": max(0, int(params.get("page") or 0)),
                "size": size, "sort": "createdAt,DESC"},
    )
    if not result.success:
        return ToolResult(
            success=False, error=f"Could not read activity for deal {code}: {result.error}"
        )

    shaped = slim_rows(result.data, ACTIVITY_FIELDS, max_rows=size)
    return ok(
        shaped,
        f"{shaped.get('total', 0)} activity entr(ies) on {code}; showing {len(shaped['rows'])}",
        max_chars=6000,
    )


async def _note_list(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Notes on one deal or one lead.

    Filtered by the numeric `ticketId` / `ownerId`, not the code: notes carry
    the parent as a plain id column, so the caller passes the id from
    `deal_get` / `lead_get` rather than the code the write tools take. The two
    differ on purpose and the parameter descriptions say which is which.
    """
    deal_id = params.get("deal_id")
    lead_id = params.get("lead_id")
    if deal_id in (None, "", 0) and lead_id in (None, "", 0):
        return ToolResult(
            success=False,
            error=(
                "Pass deal_id or lead_id — the numeric id from deal_get or "
                "lead_get, not the 22-character code."
            ),
        )

    condition = (
        filt("ticketId", deal_id) if deal_id not in (None, "", 0) else filt("ownerId", lead_id)
    )
    body = query_body(
        condition,
        page=int(params.get("page") or 0),
        size=int(params.get("size") or 20),
        sort_field="createdAt",
        sort_desc=True,
    )
    result = await client().post(
        f"{NOTES}/eager/query", headers=headers(context), json=body
    )
    if not result.success:
        return ToolResult(success=False, error=f"Could not read notes: {result.error}")

    shaped = slim_rows(result.data, NOTE_FIELDS)
    where = f"deal {deal_id}" if deal_id else f"lead {lead_id}"
    return ok(
        shaped,
        f"{shaped.get('total', 0)} note(s) on {where}; showing {len(shaped['rows'])}",
        max_chars=6000,
    )


async def _notification_list(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """The caller's own in-app notification feed.

    The notification service scopes to the signed-in user, so there is nothing
    to filter by here beyond paging — and nothing a caller could pass that
    would widen it.
    """
    result = await client().get(
        NOTIFICATIONS,
        headers=headers(context),
        params={
            "page": max(0, int(params.get("page") or 0)),
            "size": max(1, min(int(params.get("size") or 20), 50)),
            "sort": "createdAt,DESC",
        },
    )
    if not result.success:
        return ToolResult(success=False, error=f"Could not read notifications: {result.error}")

    page = result.data if isinstance(result.data, dict) else {}
    rows = []
    for entry in page.get("content") or []:
        if not isinstance(entry, dict):
            continue
        rows.append(
            {
                "title": entry.get("title"),
                "message": entry.get("message"),
                "category": entry.get("category") or entry.get("notificationCategory"),
                "read": entry.get("isRead", entry.get("read")),
                "createdAt": from_epoch(entry.get("createdAt")),
            }
        )
    return ok(
        {"notifications": rows[:20], "total": page.get("totalElements", len(rows))},
        f"{page.get('totalElements', len(rows))} notification(s)",
        max_chars=6000,
    )


# ── tool definitions ────────────────────────────────────────────────────────

deal_activity = ToolDefinition(
    name="deal_activity",
    display_name="Deal Activity",
    description=(
        "The timeline of what has happened on a deal — stage moves, "
        "reassignments, notes and tasks — newest first. This is the tool for "
        "'what's the history here', 'has anyone followed up', or 'why is this "
        "still open'. deal_get gives the current state; this gives how it got "
        "there."
    ),
    parameters=[
        ToolParameter(
            name="code", type="string", description="The deal's 22-character code.", required=True
        ),
        ToolParameter(
            name="page", type="integer", description="Zero-based page number.", required=False, default=0
        ),
        ToolParameter(
            name="size", type="integer", description="Entries per page, 1-50 (default 20).",
            required=False, default=20,
        ),
    ],
    execute=_deal_activity,
)

note_list = ToolDefinition(
    name="note_list",
    display_name="List Notes",
    description=(
        "Read the notes recorded on a deal or a lead, newest first. Takes the "
        "NUMERIC id from deal_get / lead_get, not the 22-character code."
    ),
    parameters=[
        ToolParameter(
            name="deal_id",
            type="integer",
            description="Numeric deal id, from deal_get's `id` field.",
            required=False,
        ),
        ToolParameter(
            name="lead_id",
            type="integer",
            description="Numeric lead id, from lead_get's `id` field.",
            required=False,
        ),
        ToolParameter(
            name="page", type="integer", description="Zero-based page number.", required=False, default=0
        ),
        ToolParameter(
            name="size", type="integer", description="Notes per page, 1-50 (default 20).",
            required=False, default=20,
        ),
    ],
    execute=_note_list,
)

notification_list = ToolDefinition(
    name="notification_list",
    display_name="My Notifications",
    description=(
        "The signed-in user's own in-app notification feed — new leads "
        "assigned, stage changes, and other alerts the CRM raised for them. "
        "Use it for 'what have I missed' or 'anything new for me'."
    ),
    parameters=[
        ToolParameter(
            name="page", type="integer", description="Zero-based page number.", required=False, default=0
        ),
        ToolParameter(
            name="size", type="integer", description="Notifications per page, 1-50 (default 20).",
            required=False, default=20,
        ),
    ],
    execute=_notification_list,
)

HISTORY_TOOLS = [deal_activity, note_list, notification_list]
