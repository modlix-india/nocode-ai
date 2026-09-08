"""Follow-up tools: tasks and notes against a deal or a lead.

Both are additive — nothing already recorded is changed — but both are still
confirmed, because a note is visible to the whole team and a task lands in
someone's list of work.

The one non-obvious constraint is that a Task must name a **task type**, and the
type decides whether the task hangs off a deal or off a lead
(`TaskType.contentEntitySeries`). `task_create` resolves the type by name so a
model does not have to carry a second id around, and names the valid types back
when it cannot.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.leadzump.tools._client import (
    NOTES,
    TASKS,
    and_,
    client,
    filt,
    from_epoch,
    headers,
    ok,
    query_body,
    require_code,
    slim_rows,
    to_epoch,
)
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


async def _load_task_types(context: dict[str, Any]) -> list[dict[str, Any]]:
    result = await client().get(
        f"{TASKS}/types", headers=headers(context), params={"size": 100, "sort": "name,ASC"}
    )
    if not result.success or not isinstance(result.data, dict):
        return []
    return [t for t in (result.data.get("content") or []) if isinstance(t, dict)]


async def _resolve_task_type(
    params: dict[str, Any], context: dict[str, Any]
) -> tuple[Any, ToolResult | None]:
    """The task type id to send, from either an id or a name."""
    given_id = params.get("task_type_id")
    if given_id not in (None, "", 0):
        return given_id, None

    wanted = (params.get("task_type") or "").strip().lower()
    types = await _load_task_types(context)
    if not types:
        return None, ToolResult(
            success=False,
            error=(
                "This tenant has no task types configured, so no task can be "
                "created. They are set up in the app under task settings."
            ),
        )

    names = ", ".join(str(t.get("name")) for t in types if t.get("name"))
    if not wanted:
        return None, ToolResult(
            success=False,
            error=f"task_type is required. Available types: {names}.",
        )

    for candidate in types:
        if str(candidate.get("name") or "").strip().lower() == wanted:
            return candidate.get("id"), None

    return None, ToolResult(
        success=False,
        error=f"No task type called '{params.get('task_type')}'. Available types: {names}.",
    )


def _target(params: dict[str, Any]) -> tuple[dict[str, Any], ToolResult | None]:
    """The deal or lead the content hangs off.

    `Identity` accepts a bare code string, so the 22-character code goes
    straight in. Exactly one of the two is expected: passing both is how a note
    ends up on a deal that does not belong to the lead named alongside it, and
    the backend refuses that pairing anyway.
    """
    deal = (params.get("deal_code") or "").strip()
    lead = (params.get("lead_code") or "").strip()
    if deal and lead:
        return {}, ToolResult(
            success=False,
            error="Pass deal_code or lead_code, not both — a deal already knows its lead.",
        )
    if deal:
        return {"ticketId": deal}, None
    if lead:
        return {"ownerId": lead}, None
    return {}, ToolResult(
        success=False, error="Pass deal_code (preferred) or lead_code to say what this is about."
    )


async def _note_add(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    content = (params.get("content") or "").strip()
    if not content:
        return ToolResult(success=False, error="content is required and cannot be blank.")

    target, err = _target(params)
    if err:
        return err
    for key, value in list(target.items()):
        code, code_err = require_code({key: value}, key)
        if code_err:
            return code_err

    body: dict[str, Any] = {"content": content, **target}
    if params.get("title"):
        body["name"] = params["title"]

    result = await client().post(f"{NOTES}/req", headers=headers(context), json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Could not add the note: {result.error}")

    saved = result.data if isinstance(result.data, dict) else {}
    where = params.get("deal_code") or params.get("lead_code")
    return ok(
        {"code": saved.get("code"), "createdAt": from_epoch(saved.get("createdAt"))},
        f"note added to {where}",
    )


async def _task_create(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    target, err = _target(params)
    if err:
        return err
    for key, value in list(target.items()):
        code, code_err = require_code({key: value}, key)
        if code_err:
            return code_err

    task_type_id, err = await _resolve_task_type(params, context)
    if err:
        return err

    try:
        due = to_epoch(params.get("due_date"))
    except ValueError as exc:
        return ToolResult(success=False, error=str(exc))

    body: dict[str, Any] = {"taskTypeId": task_type_id, **target}
    if params.get("title"):
        body["name"] = params["title"]
    if params.get("content"):
        body["content"] = params["content"]
    if due is not None:
        # The service refuses a due date already past, so say that plainly here
        # rather than letting it come back as a generic 400.
        body["dueDate"] = due
    if params.get("priority"):
        body["taskPriority"] = str(params["priority"]).upper()

    result = await client().post(f"{TASKS}/req", headers=headers(context), json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Could not create the task: {result.error}")

    saved = result.data if isinstance(result.data, dict) else {}
    where = params.get("deal_code") or params.get("lead_code")
    return ok(
        {
            "code": saved.get("code"),
            "name": saved.get("name"),
            "dueDate": from_epoch(saved.get("dueDate")),
        },
        f"task created on {where}",
    )


# ── tool definitions ────────────────────────────────────────────────────────

_TARGET_PARAMS = [
    ToolParameter(
        name="deal_code",
        type="string",
        description="The deal's 22-character code. Use this when the subject is an opportunity.",
        required=False,
    ),
    ToolParameter(
        name="lead_code",
        type="string",
        description="The lead's 22-character code, when the subject is the person, not a deal.",
        required=False,
    ),
]

note_add = ToolDefinition(
    name="note_add",
    display_name="Add Note",
    kind="elicitation",
    elicit_mode="blocking",
    description=(
        "Record a note on a deal or a lead. Notes are part of the shared record "
        "the whole team reads, so write what happened, not a summary of the "
        "conversation with you. Pauses for the user's confirmation."
    ),
    parameters=[
        *_TARGET_PARAMS,
        ToolParameter(name="content", type="string", description="The note text.", required=True),
        ToolParameter(
            name="title", type="string", description="Optional short heading for the note.", required=False
        ),
    ],
    execute=_note_add,
)

task_create = ToolDefinition(
    name="task_create",
    display_name="Create Task",
    kind="elicitation",
    elicit_mode="blocking",
    description=(
        "Create a follow-up task on a deal or a lead. Every task needs a task "
        "type — pass its name and it will be matched, or omit it to be told "
        "which types this tenant has. A due date must be in the future. Pauses "
        "for the user's confirmation."
    ),
    parameters=[
        *_TARGET_PARAMS,
        ToolParameter(
            name="task_type",
            type="string",
            description=(
                "Name of the task type, e.g. 'Call' or 'Site visit'. Omit it to "
                "have the tool list the types this tenant has configured."
            ),
            required=False,
        ),
        ToolParameter(
            name="task_type_id",
            type="integer",
            description="Numeric task type id, if you already have it.",
            required=False,
        ),
        ToolParameter(name="title", type="string", description="Short task title.", required=False),
        ToolParameter(
            name="content", type="string", description="What the task is, in a sentence.", required=False
        ),
        ToolParameter(
            name="due_date",
            type="string",
            description=(
                "ISO-8601 UTC date/datetime the task is due, e.g. 2026-09-12T10:00:00Z. "
                "Must be in the future."
            ),
            required=False,
        ),
        ToolParameter(
            name="priority",
            type="string",
            description="Task priority.",
            required=False,
            enum=["LOW", "MEDIUM", "HIGH"],
        ),
    ],
    execute=_task_create,
)


TASK_FIELDS = [
    "code",
    "name",
    "content",
    "taskTypeId",
    "ticketId",
    "ownerId",
    "dueDate",
    "taskPriority",
    "isCompleted",
    "createdBy",
    "createdAt",
]


async def _task_list(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Tasks, filtered the way people actually ask for them.

    "My tasks" is `mine=true`, which filters on `createdBy` rather than an
    assignee: a Task has no assignee column, and the shell's own overdue
    toaster (`getTasksData`) reads it exactly this way. Getting that wrong
    would quietly answer for the whole tenant.
    """
    clauses: list[dict | None] = []

    if params.get("mine"):
        auth = context.get("auth")
        user_id = getattr(auth, "user_id", None)
        if not user_id:
            return ToolResult(
                success=False,
                error="Cannot resolve the signed-in user, so 'mine' cannot be applied.",
            )
        clauses.append(filt("createdBy", user_id))

    if params.get("deal_id") not in (None, "", 0):
        clauses.append(filt("ticketId", params["deal_id"]))
    if params.get("lead_id") not in (None, "", 0):
        clauses.append(filt("ownerId", params["lead_id"]))

    status = (params.get("status") or "open").lower()
    if status == "open":
        clauses.append(filt("isCompleted", None, "IS_FALSE"))
    elif status == "completed":
        clauses.append(filt("isCompleted", None, "IS_TRUE"))

    try:
        due_before = to_epoch(params.get("due_before"))
    except ValueError as exc:
        return ToolResult(success=False, error=str(exc))
    if due_before is not None:
        clauses.append(filt("dueDate", due_before, "LESS_THAN_EQUAL"))

    body = query_body(
        and_(*clauses),
        page=int(params.get("page") or 0),
        size=int(params.get("size") or 20),
        sort_field="dueDate",
        sort_desc=False,
    )
    result = await client().post(f"{TASKS}/eager/query", headers=headers(context), json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Task search failed: {result.error}")

    shaped = slim_rows(result.data, TASK_FIELDS)
    return ok(
        shaped,
        f"{shaped.get('total', 0)} task(s) matched; showing {len(shaped['rows'])}",
        max_chars=6000,
    )


async def _task_complete(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Mark a task done (or reopen it).

    `completed` is a query parameter on this route, not a body field, and the
    optional `completedDate` is ISO-8601 there rather than epoch seconds —
    it binds through `@DateTimeFormat` on a `@RequestParam`, which never goes
    near the epoch-seconds Jackson module the bodies use.
    """
    code, err = require_code(params)
    if err:
        return err

    completed = params.get("completed")
    completed = True if completed is None else bool(completed)

    result = await client().put(
        f"{TASKS}/req/{code}/completed",
        headers=headers(context),
        params={"completed": str(completed).lower()},
    )
    if not result.success:
        return ToolResult(success=False, error=f"Could not update task {code}: {result.error}")

    saved = result.data if isinstance(result.data, dict) else {}
    verb = "completed" if completed else "reopened"
    return ok(
        {"code": saved.get("code") or code, "name": saved.get("name"),
         "isCompleted": saved.get("isCompleted")},
        f"task {saved.get('name') or code} {verb}",
    )


task_list = ToolDefinition(
    name="task_list",
    display_name="List Tasks",
    description=(
        "Find follow-up tasks. Defaults to open tasks; pass mine=true for the "
        "signed-in user's own, deal_id/lead_id to scope to one record, or "
        "due_before to find what is overdue. This is the tool for 'what do I "
        "need to do today' and 'what is overdue'."
    ),
    parameters=[
        ToolParameter(
            name="mine",
            type="boolean",
            description="Only tasks raised by the signed-in user.",
            required=False,
        ),
        ToolParameter(
            name="status",
            type="string",
            description="open (default), completed, or all.",
            required=False,
            enum=["open", "completed", "all"],
            default="open",
        ),
        ToolParameter(
            name="due_before",
            type="string",
            description=(
                "Only tasks due at or before this ISO-8601 UTC date/datetime. "
                "Pass today's date to find overdue work."
            ),
            required=False,
        ),
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
            name="size", type="integer", description="Tasks per page, 1-100 (default 20).",
            required=False, default=20,
        ),
    ],
    execute=_task_list,
)

task_complete = ToolDefinition(
    name="task_complete",
    display_name="Complete Task",
    kind="elicitation",
    elicit_mode="blocking",
    description=(
        "Mark a task completed, or reopen one with completed=false. Pauses for "
        "the user's confirmation."
    ),
    parameters=[
        ToolParameter(
            name="code", type="string", description="The task's 22-character code, from task_list.",
            required=True,
        ),
        ToolParameter(
            name="completed",
            type="boolean",
            description="True to complete (default), false to reopen.",
            required=False,
            default=True,
        ),
    ],
    execute=_task_complete,
)

CONTENT_TOOLS = [note_add, task_create, task_list, task_complete]
