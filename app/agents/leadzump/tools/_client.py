"""Shared plumbing for the LeadZump CRM tools.

Everything here exists because the entity-processor's wire contract has three
sharp edges that every tool in this package would otherwise re-learn:

1. **Timestamps are epoch seconds, not ISO strings.** ``commons``'s
   ``CommonsSerializationModule`` serialises every ``LocalDateTime`` as
   ``value.toEpochSecond(ZoneOffset.UTC)`` and deserialises it back the same
   way. A model that writes ``"2026-09-05T00:00:00"`` into a filter gets a
   parse failure; one that reads ``1757030400`` back and calls it a date gets
   nonsense. :func:`to_epoch` and :func:`from_epoch` are the only crossing.

   Related, and the reason no tool here sends ``x-tmz``: with that header the
   DAO reinterprets an epoch value as *wall-clock in that zone* and converts it
   to UTC (``ITimezoneDAO.convertTemporal``). Sending both a real UTC instant
   and a timezone would shift every boundary. So the tools speak UTC, say so,
   and leave the header off.

2. **The query body is a hand-rolled polymorphic tree.**
   ``AbstractConditionDeserializer`` picks ``ComplexCondition`` when it sees a
   textual ``operator`` matching AND/OR, and ``FilterCondition`` when it sees a
   ``field``. Nothing declares which is which. :func:`and_`, :func:`or_` and
   :func:`filt` build the two shapes so a tool never hand-writes one.

3. **A result has to fit in ~4000 characters.** A page of 25 eager deals is
   several times that, and the run loop truncates from the end — which is worse
   than dropping rows, because the model cannot tell that it is reading half a
   record. :func:`slim_rows` cuts each row to the fields a CRM answer actually
   needs and says how many rows were left out.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.core.tools.base import ToolResult
from app.core.tools.http_client import SaasClient

logger = logging.getLogger(__name__)

# Entity-processor route roots. Note `owners` is the Lead track and `tickets`
# is the Deal track — the endpoint names and the UI names do not match, and
# that mismatch is the single most common way to write a wrong tool here.
EP = "/api/entity/processor"
OWNERS = f"{EP}/owners"
TICKETS = f"{EP}/tickets"
PRODUCTS = f"{EP}/products"
TEMPLATES = f"{EP}/products/templates"
STAGES = f"{EP}/stages"
TASKS = f"{EP}/tasks"
NOTES = f"{EP}/notes"
ANALYTICS = f"{EP}/analytics/tickets"
SOURCES = f"{EP}/sources"
ACTIVITIES = f"{EP}/activities"
NOTIFICATIONS = "/api/notification/notifications"

# LeadZump's own KIRun server functions, reached through core's executor:
#   POST /api/core/function/execute/{namespace}/{name}  body = {argName: value}
#   -> [{"name": "output", "result": {...}}]  or {"name": "error", ...}
#
# Worth the extra hop over calling entity-processor directly: the `…AndSN`
# family raises the app's own in-app notification after the write, which the
# REST route does not. A deal the agent creates or moves should reach the
# assignee's notification feed exactly as one created in the UI does.
FUNCTION_EXEC = "/api/core/function/execute"

# `BaseUpdatableDto.CODE_LENGTH`. A 22-character string is a code; anything
# else that parses as an integer is an id. `Identity`'s deserializer applies
# exactly this rule, so a tool that guesses differently sends the wrong thing.
CODE_LENGTH = 22

_client_instance: SaasClient | None = None


def client() -> SaasClient:
    """Shared SaasClient for the gateway."""
    global _client_instance
    if _client_instance is None:
        _client_instance = SaasClient(settings.GATEWAY_URL)
    return _client_instance


async def close_leadzump_client() -> None:
    """Close the SaasClient (call on shutdown)."""
    global _client_instance
    if _client_instance is not None:
        await _client_instance.close()
        _client_instance = None


def headers(context: dict[str, Any]) -> dict[str, str]:
    """Auth headers for the caller.

    Deliberately the ONLY way a tool gets a tenant. `ProcessorAccess.of(ca, …)`
    builds the server's access context from this token's own client, and for a
    business-partner user resolves it through `getEffectiveClientCode()`. A tool
    that accepted a client code as an argument would let a model widen its own
    reach by asking for a different one, so none of them do.
    """
    return dict(context.get("headers") or {})


# ── condition tree ──────────────────────────────────────────────────────────


def filt(field: str, value: Any = None, operator: str = "EQUALS", **extra: Any) -> dict:
    """One FilterCondition node.

    `operator` is a `FilterConditionOperator` name: EQUALS, LIKE, IN, BETWEEN,
    GREATER_THAN_EQUAL, LESS_THAN_EQUAL, IS_NULL, IS_TRUE, IS_FALSE,
    STRING_LOOSE_EQUAL, TEXT_SEARCH.
    """
    node: dict[str, Any] = {"field": field, "operator": operator}
    if value is not None:
        node["value"] = value
    node.update(extra)
    return node


def and_(*conditions: dict | None) -> dict | None:
    """AND the non-empty conditions, collapsing a single one."""
    return _complex("AND", conditions)


def or_(*conditions: dict | None) -> dict | None:
    """OR the non-empty conditions, collapsing a single one."""
    return _complex("OR", conditions)


def _complex(operator: str, conditions) -> dict | None:
    kept = [c for c in conditions if c]
    if not kept:
        return None
    if len(kept) == 1:
        return kept[0]
    return {"operator": operator, "conditions": kept}


def query_body(
    condition: dict | None,
    page: int = 0,
    size: int = 20,
    sort_field: str = "updatedAt",
    sort_desc: bool = True,
    eager: bool = True,
    eager_fields: list[str] | None = None,
) -> dict[str, Any]:
    """A `com.fincity.saas.commons.model.Query` body.

    `sort` is a Spring `Sort`, deserialised by commons' `SortSerializationModule`
    from the `[{property, direction}]` array shape — the same shape the LeadZump
    pages send.
    """
    body: dict[str, Any] = {
        "page": max(0, page),
        "size": max(1, min(size, 100)),
        "sort": [{"property": sort_field, "direction": "DESC" if sort_desc else "ASC"}],
        "eager": bool(eager),
    }
    if condition:
        body["condition"] = condition
    if eager_fields:
        body["eagerFields"] = eager_fields
    return body


# ── time ────────────────────────────────────────────────────────────────────


def to_epoch(value: str | int | None) -> int | None:
    """An ISO-8601 date or datetime to epoch seconds, UTC.

    A bare date means midnight at the start of that day. A value with no offset
    is read as UTC, because that is what the backend stores and what every
    timestamp it returns means. Returns None for None; raises ValueError on
    anything it cannot parse, so a bad filter fails loudly instead of silently
    matching everything.
    """
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    normalised = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError as exc:
        raise ValueError(
            f"'{value}' is not an ISO-8601 date or datetime "
            f"(expected e.g. 2026-09-05 or 2026-09-05T14:30:00Z)."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def from_epoch(value: Any) -> Any:
    """Epoch seconds back to an ISO-8601 UTC string, for anything model-facing.

    Passes through whatever it does not recognise: the eager payloads mix
    resolved relation objects in with the scalar columns, and a resolver that
    threw away anything unexpected would quietly drop names.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OSError, OverflowError):
        return value


# ── shaping results for the model ───────────────────────────────────────────


def label(value: Any) -> Any:
    """A relation's human name, or the raw value when it is a plain column.

    `?eager=true` replaces `productId`, `stage`, `status`, `assignedUserId` and
    friends with resolved objects. Which key carries the name differs by
    resolver — stages and products have `name`, users have first/last — so this
    tries them in order rather than assuming.
    """
    if not isinstance(value, dict):
        return value
    for key in ("name", "title", "code"):
        if value.get(key):
            return value[key]
    first = (value.get("firstName") or "").strip()
    last = (value.get("lastName") or "").strip()
    if first or last:
        return f"{first} {last}".strip()
    return value.get("userName") or value.get("emailId") or value.get("id")


_TIME_FIELDS = {
    "createdAt",
    "updatedAt",
    "expiresOn",
    "dueDate",
    "nextReminder",
    "completedDate",
    "latestTaskDueDate",
    "lastMessageAt",
    "whatsappOptedOutAt",
}


def humanise(row: dict) -> dict[str, Any]:
    """A whole record made readable, without mangling what is not a date.

    The obvious version — ``{k: from_epoch(v) for k, v in row.items()}`` — is
    wrong, and quietly: `from_epoch` cannot tell an epoch from any other
    integer, so a deal's `id` of 3457 came back as "1970-01-01T00:57:37Z" and
    `dialCode` 91 as a moment in 1970. Only the fields that really are
    timestamps get converted; relations get labelled; everything else is left
    exactly as it arrived.
    """
    out: dict[str, Any] = {}
    for key, value in row.items():
        if value is None or value == "":
            continue
        out[key] = from_epoch(value) if key in _TIME_FIELDS else label(value)
    return out


def slim(row: dict, fields: list[str]) -> dict:
    """One row cut to `fields`, with relations labelled and times readable."""
    out: dict[str, Any] = {}
    for field in fields:
        if field not in row:
            continue
        value = row[field]
        if value is None or value == "":
            continue
        out[field] = from_epoch(value) if field in _TIME_FIELDS else label(value)
    return out


# How many characters of rendered rows one result may spend. Below the tool's
# own `max_result_chars` with room for the headline and the paging envelope, so
# the framework's truncation never fires on a search.
ROW_CHAR_BUDGET = 4800


def slim_rows(
    page: Any,
    fields: list[str],
    max_rows: int = 15,
    char_budget: int = ROW_CHAR_BUDGET,
) -> dict[str, Any]:
    """A Spring `Page` payload reduced to something that fits a tool result.

    Budgeted by characters, not only by a row count, because a row's width is
    not fixed: a deal with a long product name, an assignee and a comment is
    three times one with a phone number and nothing else. Fifteen of the former
    overflow the cap, and what happens then is the bad case — the run loop
    truncates from the end, mid-record, and the model cannot tell that the
    record it is reading is half a record. Dropping whole rows and saying so is
    strictly better, so that is what this does.

    Keeps the paging counts, because "which fifteen of how many" is the part a
    model most often gets wrong when it can see only rows.
    """
    if not isinstance(page, dict):
        return {"rows": [], "note": "unexpected response shape"}
    content = page.get("content")
    if not isinstance(content, list):
        content = []
    rows = [slim(r, fields) for r in content if isinstance(r, dict)]

    kept = budget_rows(rows, max_rows=max_rows, char_budget=char_budget)

    out: dict[str, Any] = {
        "rows": kept,
        "total": page.get("totalElements", len(rows)),
        "page": page.get("number", 0),
        "size": page.get("size", len(rows)),
    }
    if len(kept) < len(rows):
        out["note"] = (
            f"showing {len(kept)} of {len(rows)} rows on this page (result size limit); "
            f"narrow the filter or ask for the next page"
        )
    return out


def budget_rows(
    rows: list[dict[str, Any]],
    max_rows: int = 15,
    char_budget: int = ROW_CHAR_BUDGET,
) -> list[dict[str, Any]]:
    """As many whole rows as fit the budget.

    Measured with the same ``indent=2`` :func:`ok` renders with. Measuring
    compact JSON and rendering pretty was the first version, and it undercounts
    by roughly a third — enough that a full page of deals still overflowed the
    cap and was truncated mid-record, which is the exact outcome the budget
    exists to prevent.
    """
    kept: list[dict[str, Any]] = []
    used = 0
    for row in rows[:max_rows]:
        width = len(json.dumps(row, indent=2, default=str))
        # Always keep the first row, however wide: one whole record beats none.
        if kept and used + width > char_budget:
            break
        kept.append(row)
        used += width
    return kept


def ok(data: Any, headline: str, max_chars: int | None = None) -> ToolResult:
    """A successful read, shaped so the model actually receives the rows.

    ``ToolResult.to_tool_result_content`` renders ``summary`` **instead of**
    ``data`` — ``data`` is only the fallback when no summary is set. So a tool
    that pairs a one-line summary with a page of rows hands the model the line
    and silently drops the rows. That is not hypothetical: the first live run of
    this agent answered "the breakdown tool is only returning headline counts",
    three times, and refused to quote numbers it could not see. It was right.

    The convention every appbuilder read tool follows is to render the payload
    into ``summary``; ``data`` is kept alongside it for callers that read the
    result structurally (the smoke harness, the tests) rather than as text.
    """
    body = json.dumps(data, indent=2, default=str)
    return ToolResult(
        success=True,
        data=data,
        summary=f"{headline}\n{body}",
        max_result_chars=max_chars,
    )


async def call_server_function(
    name: str, args: dict[str, Any], context: dict[str, Any]
) -> tuple[Any, str | None]:
    """Run one of LeadZump's KIRun server functions.

    Returns ``(result, error)``. The executor answers with an event list —
    ``[{"name": "output", "result": {...}}]`` on success — and a function that
    raised comes back as a non-2xx or an event named ``error``, so both are
    unwrapped here rather than at each call site.
    """
    response = await client().post(
        f"{FUNCTION_EXEC}/{name}", headers=headers(context), json=args
    )
    if not response.success:
        return None, response.error or f"{name} failed"

    events = response.data
    if isinstance(events, str):
        try:
            events = json.loads(events)
        except ValueError:
            return events, None
    if not isinstance(events, list):
        return events, None

    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("name") == "error":
            return None, str(event.get("result") or event)
        if event.get("name") == "output":
            return event.get("result"), None
    return events, None


def not_found(entity: str, code: str) -> ToolResult:
    return ToolResult(
        success=False,
        error=(
            f"No {entity} with code '{code}' is visible to you. Check the code, "
            f"or search for it first — a code you were not given by a search may "
            f"belong to another tenant, in which case it will never resolve."
        ),
    )


def require_code(params: dict[str, Any], key: str = "code") -> tuple[str, ToolResult | None]:
    """Pull and sanity-check an entity code from the tool arguments."""
    code = str(params.get(key) or "").strip()
    if not code:
        return "", ToolResult(success=False, error=f"{key} is required.")
    if len(code) != CODE_LENGTH and not code.isdigit():
        return "", ToolResult(
            success=False,
            error=(
                f"'{code}' is not a valid {key}. A LeadZump record code is "
                f"{CODE_LENGTH} characters; pass the code exactly as a search "
                f"returned it, or its numeric id."
            ),
        )
    return code, None
