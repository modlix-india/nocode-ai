"""Lore as agent tools.

An agent gets six verbs:

  lore_brief   — what do I need to know before touching this app
  lore_search  — has anyone established anything about X
  lore_about   — everything known about one page / storage / function
  lore_add     — the user stated a fact; record it as a typed, pinned entry now
  lore_note    — I saw something worth keeping; let the curator decide what it means
  lore_correct — that entry is wrong, here is the truth

`lore_add` and `lore_note` are the two write paths and the difference matters.
`add` is for a claim the user made outright, where there is nothing to infer and
waiting for a curation pass would be absurd. `note` is for evidence: something
that looks significant but whose durable meaning the curator should work out
against everything else it knows.

Reads are cheap and safe, so the agent is encouraged to call `lore_brief`
once at the start of a task. Writes go in as OBSERVATIONS, never directly as
entries: even when an agent is sure, the claim goes through curation so that it
gets provenance, deduplication and a confidence score like everything else.

The one exception is `lore_correct`, which pins the corrected entry. A person
saying "no, it works like this" is the highest-quality signal lore can get,
and pinning is what stops the curator from quietly undoing it later.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.services.lore import access, ingest, retrieval, store
from app.services.lore.access import LoreAccessError, LoreScope
from app.services.lore.curator import redact
from app.services.lore.models import ENTRY_KINDS, ENTRY_KIND_HELP, normalise_subject

logger = logging.getLogger(__name__)


class _Auth:
    """Minimal shape `access.resolve_scope` needs, built from a tool context."""

    def __init__(self, client_code: str) -> None:
        self.client_code = client_code


def _tenant(context: dict[str, Any]) -> tuple[str, str, str | None]:
    """(client_code, app_code, error) from the session context.

    The client code is the LOGGED-IN user's, never the app owner's: an agent
    acting for a CLIENTA user writes CLIENTA lore even on a SYSTEM-owned app.
    """
    auth = context.get("auth")
    headers = context.get("headers") or {}
    client_code = ""
    if auth and getattr(auth, "client_code", None):
        client_code = auth.client_code
    elif headers.get("clientCode"):
        client_code = headers["clientCode"]
    # Follows the session's focus app: lore recorded while building `crm` is
    # knowledge about `crm`, even in a session opened from appbuilder.
    from app.core.session import FOCUS_APP_KEY
    app_code = (
        (context.get(FOCUS_APP_KEY) or "").strip()
        or context.get("app_code")
        or (getattr(auth, "app_code", "") if auth else "")
        or ""
    )
    if not client_code or not app_code:
        return client_code, app_code, (
            "Missing tenant context: lore is scoped per (client, app), and this "
            "session has no clientCode/app_code."
        )
    return client_code, app_code, None


async def _scope(context: dict[str, Any], *, for_write: bool) -> tuple[LoreScope | None, ToolResult | None]:
    """Resolve the caller's lore scope, or return the ToolResult to hand back.

    Agents run on behalf of a signed-in person and inherit exactly their access:
    an agent may not write knowledge into an app its user cannot edit.
    """
    client_code, app_code, error = _tenant(context)
    if error:
        return None, ToolResult(success=False, error=error)
    try:
        scope = await access.resolve_scope(_Auth(client_code), app_code)
        if for_write:
            scope.require_write()
        else:
            scope.require_read()
        return scope, None
    except LoreAccessError as exc:
        return None, ToolResult(success=False, error=exc.message)


def _user_id(context: dict[str, Any]) -> int:
    auth = context.get("auth")
    try:
        return int(getattr(auth, "user_id", 0) or 0)
    except (TypeError, ValueError):
        return 0


# ── Reads ────────────────────────────────────────────────────────────────


async def _brief(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    scope, refusal = await _scope(context, for_write=False)
    if refusal:
        return refusal

    result = await retrieval.brief(
        scope,
        subject=params.get("subject") or None,
        budget=int(params.get("budget") or retrieval.BRIEF_BUDGET),
    )
    if result["entry_count"] == 0:
        return ToolResult(
            success=True,
            data={"markdown": result["markdown"], "entry_count": 0},
            summary=(
                "Lore has nothing recorded about this app yet. Work normally, and "
                "use lore_note for anything worth keeping."
            ),
        )
    return ToolResult(
        success=True,
        data=result,
        summary=result["markdown"],
        model_summary=result["markdown"],
    )


async def _search(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    query = (params.get("query") or "").strip()
    if not query:
        return ToolResult(success=False, error="query is required")

    scope, refusal = await _scope(context, for_write=False)
    if refusal:
        return refusal

    kinds = params.get("kinds") or None
    result = await retrieval.search(
        scope, query,
        limit=int(params.get("limit") or 12),
        kinds=kinds if isinstance(kinds, list) else None,
    )
    if not result["count"]:
        return ToolResult(
            success=True, data=result,
            summary=f"Nothing recorded about “{query}” in this app.",
        )
    lines = [
        f"#{r['id']} [{r['kind']}] {r['subject']} · confidence {r['effective_confidence']}\n"
        f"  {r['title']}\n  {r['body'][:400]}"
        for r in result["results"]
    ]
    return ToolResult(
        success=True, data=result,
        summary=f"{result['count']} match(es) for “{query}”:\n\n" + "\n\n".join(lines),
    )


async def _about(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    scope, refusal = await _scope(context, for_write=False)
    if refusal:
        return refusal

    subject = normalise_subject(params.get("subject"))
    if subject == "app" and params.get("subject"):
        return ToolResult(
            success=False,
            error=(
                f"'{params.get('subject')}' is not a recognisable subject. Use "
                "'<type>:<name>', e.g. 'page:jobsToday' or 'storage:job'."
            ),
        )
    result = await retrieval.about(scope, subject)
    if not result["count"]:
        return ToolResult(
            success=True, data=result,
            summary=f"Nothing recorded about `{subject}` yet.",
        )

    def render(entries: list[dict[str, Any]]) -> str:
        return "\n\n".join(
            f"#{e['id']} [{e['kind']}] confidence {e['effective_confidence']}"
            f"{' PINNED' if e['pinned'] else ''}\n  {e['title']}\n  {e['body']}"
            for e in entries
        )

    parts = [f"Known about `{subject}`:", render(result["direct"])]
    if result["related"]:
        parts += ["\nRelated entries elsewhere in the app:", render(result["related"])]
    return ToolResult(success=True, data=result, summary="\n".join(p for p in parts if p))


# ── Writes ───────────────────────────────────────────────────────────────


async def _note(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    text = (params.get("text") or "").strip()
    if len(text) < 10:
        return ToolResult(success=False, error="text is too short to be worth recording")

    scope, refusal = await _scope(context, for_write=True)
    if refusal:
        return refusal

    result = await ingest.note(
        scope.client_code, scope.app_code,
        text=text,
        subject=params.get("subject") or "app",
        author=params.get("author") or "agent",
        user_id=_user_id(context),
    )
    if not result.get("observation_id"):
        return ToolResult(success=False, error="Could not record the note (lore storage unavailable)")

    repeated = result.get("seen_count", 1) > 1
    return ToolResult(
        success=True,
        data=result,
        summary=(
            "Already recorded; noted that it came up again."
            if repeated else
            "Recorded. It will be curated into the app's knowledge on the next pass."
        ),
    )


async def _add(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Write a durable entry directly, skipping observation and curation.

    `lore_note` records evidence and lets the curator decide what it means.
    This is for when the user states a fact outright: "technicians must never
    see pricing". There is nothing to infer, and making them wait for a
    curation pass to see their own words recorded would be absurd.
    """
    kind = str(params.get("kind") or "").strip()
    if kind not in ENTRY_KINDS:
        return ToolResult(
            success=False,
            error=f"kind must be one of: {', '.join(ENTRY_KINDS)}",
        )
    title = str(params.get("title") or "").strip().rstrip(".")
    body = str(params.get("body") or "").strip()
    if len(title) < 4:
        return ToolResult(success=False, error="title must be a real one-line statement")
    if len(body) < 10:
        return ToolResult(success=False, error="body must say something; one to four sentences")

    scope, refusal = await _scope(context, for_write=True)
    if refusal:
        return refusal

    user_id = _user_id(context)
    result = await store.add_entry(
        scope.client_code, scope.app_code,
        kind=kind,
        title=title[:240],
        body=redact(body)[:20000],
        subject=params.get("subject") or "app",
        tags=[str(t)[:40] for t in (params.get("tags") or [])][:8],
        confidence=int(params.get("confidence") or 90),
        pinned=True,
        created_by=user_id,
    )
    await store.set_pinned(result["id"], True, updated_by=user_id)

    return ToolResult(
        success=True,
        data={"entry_id": result["id"], "created": result["created"], "kind": kind},
        summary=(
            f"Recorded as {kind} #{result['id']}: “{title}”. Pinned, so automatic "
            "curation will not change it."
            if result["created"] else
            f"That was already recorded as #{result['id']}; confirmed it rather than duplicating."
        ),
    )


async def _correct(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Fix a wrong entry and pin the correction so curation cannot undo it.

    If the entry is INHERITED from the app owner, the correction becomes an
    override in this client rather than a rewrite of the owner's row. Telling
    the user which happened matters: "fixed for everyone" and "fixed for us"
    are different promises.
    """
    scope, refusal = await _scope(context, for_write=True)
    if refusal:
        return refusal

    try:
        entry_id = int(params.get("entry_id"))
    except (TypeError, ValueError):
        return ToolResult(success=False, error="entry_id must be a number (see lore_search results)")

    entry = await store.get_entry(entry_id)
    if not entry or entry.app_code != scope.app_code or entry.client_code not in scope.read_chain:
        return ToolResult(success=False, error=f"No lore entry {entry_id} in this app")

    user_id = _user_id(context)
    inherited = not scope.owns(entry.client_code)
    correction = (params.get("correction") or "").strip()

    if not correction:
        # No replacement text means the claim is simply false.
        outcome = await store.retire_in_scope(entry, scope, updated_by=user_id)
        hidden = outcome["action"] == "hidden"
        return ToolResult(
            success=outcome["action"] != "missing",
            data={"entry_id": outcome["id"], "action": outcome["action"],
                  "inherited": inherited},
            summary=(
                f"Entry #{entry_id} (“{entry.title}”) came from {entry.client_code} and cannot be "
                f"deleted from here, so it is now hidden for {scope.client_code} only."
                if hidden else
                f"Retired entry #{entry_id}: “{entry.title}”."
            ),
            error="" if outcome["action"] != "missing" else "Could not retire that entry",
        )

    outcome = await store.edit_in_scope(
        entry, scope,
        title=params.get("title") or None,
        body=redact(correction)[:20000],
        confidence=95,
        updated_by=user_id,
        message="corrected by a person",
    )
    if outcome["action"] == "missing":
        return ToolResult(success=False, error=f"Entry #{entry_id} no longer exists")

    await store.set_pinned(outcome["id"], True, updated_by=user_id)
    forked = outcome["action"] == "forked"
    return ToolResult(
        success=True,
        data={**outcome, "inherited": inherited},
        summary=(
            f"Entry #{entry_id} belongs to {entry.client_code}, so the correction was saved as "
            f"{scope.client_code}'s own version (#{outcome['id']}). {entry.client_code} still sees "
            "the original; you will see yours from now on."
            if forked else
            f"Corrected entry #{entry_id} and pinned it, so curation will not change it again "
            "without a person."
        ),
    )


# ── Tool definitions ─────────────────────────────────────────────────────

LORE_BRIEF = ToolDefinition(
    name="lore_brief",
    display_name="App knowledge briefing",
    description=(
        "Read what is already known about this application before you change it: its "
        "purpose, the rules that must hold, the conventions it follows, past decisions "
        "and why, known traps, and what is in flight. Call this ONCE at the start of a "
        "task on an app you have not worked on in this session. Pass `subject` to "
        "narrow it to one object (e.g. 'page:jobsToday')."
    ),
    parameters=[
        ToolParameter(
            name="subject", type="string", required=False,
            description="Narrow to one object: '<type>:<name>' such as 'storage:job'. Omit for the whole app.",
        ),
        ToolParameter(
            name="budget", type="integer", required=False, default=6000,
            description="Maximum characters of briefing to return.",
        ),
    ],
    execute=_brief,
)

LORE_SEARCH = ToolDefinition(
    name="lore_search",
    display_name="Search app knowledge",
    description=(
        "Search this app's accumulated knowledge for a specific question: has a decision "
        "already been made about this, is there a convention for it, has someone hit this "
        "problem before. Use before deciding anything that feels like it might already "
        "have been decided."
    ),
    parameters=[
        ToolParameter(name="query", type="string", description="What you want to know about.", required=True),
        ToolParameter(
            name="kinds", type="array", required=False,
            description=f"Restrict to these entry kinds. One or more of: {', '.join(ENTRY_KINDS)}",
            items={"type": "string", "enum": list(ENTRY_KINDS)},
        ),
        ToolParameter(name="limit", type="integer", required=False, default=12, description="Max results."),
    ],
    execute=_search,
)

LORE_ABOUT = ToolDefinition(
    name="lore_about",
    display_name="What is known about this object",
    description=(
        "Everything recorded about one page, storage, function or other object, plus "
        "entries elsewhere in the app that mention it. Use before editing an object you "
        "did not create."
    ),
    parameters=[
        ToolParameter(
            name="subject", type="string", required=True,
            description="'<type>:<name>', e.g. 'page:jobsToday', 'storage:job', 'function:notifyLateJobs'.",
        ),
    ],
    execute=_about,
)

LORE_ADD = ToolDefinition(
    name="lore_add",
    display_name="Record app knowledge as an entry",
    description=(
        "Write a durable fact about this app straight into its knowledge, as a typed, "
        "pinned entry. Use this when the user STATES something rather than implies it: a "
        "rule, a decision and its reason, a convention they want followed, a term and what "
        "it means here. Prefer this over lore_note whenever you already know the kind and "
        "can write the claim in one line. Do NOT record secrets, tokens or customer data.\n"
        "Kinds:\n"
        + "\n".join(f"  {k}: {v}" for k, v in ENTRY_KIND_HELP.items())
    ),
    parameters=[
        ToolParameter(
            name="kind", type="string", required=True,
            description="Which kind of knowledge this is.",
            enum=list(ENTRY_KINDS),
        ),
        ToolParameter(
            name="title", type="string", required=True,
            description="One line, readable on its own, no trailing full stop.",
        ),
        ToolParameter(
            name="body", type="string", required=True,
            description="One to four sentences. For a decision, always include the why.",
        ),
        ToolParameter(
            name="subject", type="string", required=False, default="app",
            description="What it is about: 'app' or '<type>:<name>' like 'storage:job'.",
        ),
        ToolParameter(
            name="tags", type="array", required=False,
            description="Optional short tags.",
            items={"type": "string"},
        ),
        ToolParameter(
            name="confidence", type="integer", required=False, default=90,
            description="0-100. 90 when the user stated it plainly; lower if you are inferring.",
        ),
    ],
    execute=_add,
)

LORE_NOTE = ToolDefinition(
    name="lore_note",
    display_name="Record app knowledge",
    description=(
        "Write down something about this app that should outlive the conversation: a "
        "decision and its reason, a convention, a constraint the user stated, a trap you "
        "hit, who asked for what. Record the durable claim, not a summary of the chat. "
        "Do NOT record secrets, tokens or customer data. Call this when the user tells "
        "you something that will still matter next month."
    ),
    parameters=[
        ToolParameter(
            name="text", type="string", required=True,
            description="The thing worth keeping, in one to four sentences. Include the reasoning.",
        ),
        ToolParameter(
            name="subject", type="string", required=False, default="app",
            description="What it is about: 'app' or '<type>:<name>'.",
        ),
        ToolParameter(
            name="author", type="string", required=False, default="agent",
            description="'user' when relaying something the person said, 'agent' when it is your own finding.",
        ),
    ],
    execute=_note,
)

LORE_CORRECT = ToolDefinition(
    name="lore_correct",
    display_name="Correct app knowledge",
    description=(
        "Fix a lore entry the user says is wrong. Give `correction` to replace its body "
        "(the entry is then pinned so automatic curation cannot change it back), or omit "
        "`correction` to retire an entry that is simply no longer true. Get the entry id "
        "from lore_search or lore_about."
    ),
    parameters=[
        ToolParameter(name="entry_id", type="integer", required=True, description="The entry to fix."),
        ToolParameter(
            name="correction", type="string", required=False,
            description="The corrected knowledge. Omit to retire the entry instead.",
        ),
        ToolParameter(name="title", type="string", required=False, description="Optional replacement title."),
    ],
    execute=_correct,
)

# Read-only set — safe to hand to any agent.
LORE_READ_TOOLS: list[ToolDefinition] = [LORE_BRIEF, LORE_SEARCH, LORE_ABOUT]

# Everything, for agents that are building the app and should contribute back.
LORE_TOOLS: list[ToolDefinition] = LORE_READ_TOOLS + [LORE_ADD, LORE_NOTE, LORE_CORRECT]
