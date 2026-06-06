"""Per-app KB tools — read, propose, commit, search, history.

Persistence layer: app/services/app_kb.py (MySQL via cfa_app_kb).

Write flow (propose-then-confirm):
  1. Agent calls `propose_kb_update(section, body, message)`.
  2. Tool produces a unified diff vs the current latest row, stashes a
     pending-write UUID on the session, and returns the diff + UUID.
  3. User reads the diff in the stream and replies "yes" (or clicks).
  4. Agent calls `commit_kb_update(pending_uuid)`.
  5. Tool reads the stashed write, calls insert_version, returns success.

Optimistic-lock: the proposed write captures the latest version at propose
time. If a different agent commits before us, our commit retries the diff
against the new latest and fails cleanly so the agent can re-propose.

Reads:
  - kb_app_get(section)         — current body of one section
  - kb_app_history(section)     — recent versions, newest first
  - kb_app_search(query)        — FULLTEXT across latest rows
  - kb_app_list_sections()      — which sections have content for this app
"""

from __future__ import annotations

import difflib
import logging
import uuid
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.services import app_kb

logger = logging.getLogger(__name__)


# Session-stash key for pending writes that haven't been confirmed yet.
_PENDING_KEY = "pending_kb_updates"


def _resolve_tenant(context: dict[str, Any]) -> tuple[str, str, str | None]:
    """Return (client_code, app_code, error) from session context.

    The CFA agent sets both on `context` at request-handler time: client_code
    comes from the JWT, app_code comes from the chat request body.
    """
    auth = context.get("auth")
    headers = context.get("headers") or {}
    client_code = ""
    if auth and getattr(auth, "client_code", None):
        client_code = auth.client_code
    elif headers.get("clientCode"):
        client_code = headers["clientCode"]
    app_code = context.get("app_code") or (auth.app_code if auth else "") or ""
    if not client_code or not app_code:
        return client_code, app_code, (
            "Missing tenant context. The session needs a JWT (clientCode) and "
            "an explicit `app_code` on the chat request before per-app KB tools "
            "can resolve which app to read/write."
        )
    return client_code, app_code, None


def _user_id_from_context(context: dict[str, Any]) -> int:
    auth = context.get("auth")
    if auth and getattr(auth, "user_id", None):
        return int(auth.user_id)
    return 0


# ── kb_app_get ───────────────────────────────────────────────────────────


async def _execute_kb_app_get(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    section = (params.get("section") or "").strip()
    err = app_kb.validate_section(section)
    if err:
        return ToolResult(success=False, error=err)
    client_code, app_code, terr = _resolve_tenant(context)
    if terr:
        return ToolResult(success=False, error=terr)

    row = await app_kb.get_latest(client_code, app_code, section)
    if row is None:
        return ToolResult(
            success=True,
            summary=f"(no '{section}' yet for {client_code}/{app_code} — propose one with `propose_kb_update`)",
        )
    body = row.get("BODY") or ""
    head = (
        f"## {section}  (v{row.get('VERSION')}, by userId={row.get('UPDATED_BY')}, "
        f"at {row.get('UPDATED_AT')})"
    )
    note = row.get("MESSAGE")
    if note:
        head += f"\nMessage: {note}"
    return ToolResult(success=True, summary=f"{head}\n\n{body}")


kb_app_get_tool = ToolDefinition(
    name="kb_app_get",
    description=(
        "Read the current state of one per-app KB section for the app being "
        "built in this conversation. Sections: overview, current_focus, "
        "inventory, conventions, roadmap, decisions_log. The latest version "
        "is returned with author + timestamp + commit message."
    ),
    parameters=[
        ToolParameter(
            name="section", type="string",
            description="One of: overview, current_focus, inventory, conventions, roadmap, decisions_log",
            enum=list(app_kb.SECTIONS),
        ),
    ],
    execute=_execute_kb_app_get,
)


# ── kb_app_history ───────────────────────────────────────────────────────


async def _execute_kb_app_history(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    section = (params.get("section") or "").strip()
    err = app_kb.validate_section(section)
    if err:
        return ToolResult(success=False, error=err)
    try:
        limit = max(1, min(int(params.get("limit") or 10), 200))
    except (TypeError, ValueError):
        limit = 10
    client_code, app_code, terr = _resolve_tenant(context)
    if terr:
        return ToolResult(success=False, error=terr)

    rows = await app_kb.list_history(client_code, app_code, section, limit=limit)
    if not rows:
        return ToolResult(success=True, summary=f"(no history for '{section}')")
    lines = [f"History of '{section}' for {client_code}/{app_code} (latest {len(rows)}):", ""]
    for r in rows:
        lines.append(
            f"  v{r.get('VERSION')} — userId={r.get('UPDATED_BY')} — {r.get('UPDATED_AT')} — {r.get('MESSAGE') or '(no message)'}"
        )
    return ToolResult(success=True, summary="\n".join(lines))


kb_app_history_tool = ToolDefinition(
    name="kb_app_history",
    description=(
        "List recent versions of a per-app KB section (newest first) with "
        "author, timestamp, and commit message. Use to see who changed what "
        "when, or to identify a version number to read with kb_app_get."
    ),
    parameters=[
        ToolParameter(name="section", type="string", enum=list(app_kb.SECTIONS), description="Section name."),
        ToolParameter(name="limit", type="integer", required=False, default=10, description="Max versions to return (capped at 200)."),
    ],
    execute=_execute_kb_app_history,
)


# ── kb_app_search ────────────────────────────────────────────────────────


async def _execute_kb_app_search(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    query = (params.get("query") or "").strip()
    if not query:
        return ToolResult(success=False, error="`query` is required")
    try:
        limit = max(1, min(int(params.get("limit") or 10), 50))
    except (TypeError, ValueError):
        limit = 10
    client_code, app_code, terr = _resolve_tenant(context)
    if terr:
        return ToolResult(success=False, error=terr)

    matches = await app_kb.search(client_code, app_code, query, limit=limit)
    if not matches:
        return ToolResult(success=True, summary=f"(no matches for '{query}' in {client_code}/{app_code}'s KB)")
    lines = [f"KB search '{query}' ({len(matches)} hits):", ""]
    for m in matches:
        lines.append(
            f"- {m['SECTION']} (v{m['VERSION']}, score={m.get('score', 0):.2f}): "
            + (m.get("snippet") or "").replace("\n", " ")[:200]
        )
    return ToolResult(success=True, summary="\n".join(lines))


kb_app_search_tool = ToolDefinition(
    name="kb_app_search",
    description=(
        "FULLTEXT search across the latest version of every per-app KB "
        "section for this app. Returns section + snippet + relevance score. "
        "Use to locate which section to kb_app_get when you remember a "
        "phrase but not the section name."
    ),
    parameters=[
        ToolParameter(name="query", type="string", description="Natural-language search phrase."),
        ToolParameter(name="limit", type="integer", required=False, default=10, description="Max matches (capped at 50)."),
    ],
    execute=_execute_kb_app_search,
)


# ── kb_app_list_sections ─────────────────────────────────────────────────


async def _execute_kb_app_list_sections(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    client_code, app_code, terr = _resolve_tenant(context)
    if terr:
        return ToolResult(success=False, error=terr)
    present = await app_kb.list_sections_present(client_code, app_code)
    missing = [s for s in app_kb.SECTIONS if s not in present]
    lines = [f"Per-app KB for {client_code}/{app_code}:"]
    lines.append("  present: " + (", ".join(present) if present else "(none — empty KB)"))
    lines.append("  missing: " + (", ".join(missing) if missing else "(all sections populated)"))
    return ToolResult(success=True, summary="\n".join(lines))


kb_app_list_sections_tool = ToolDefinition(
    name="kb_app_list_sections",
    description=(
        "Show which per-app KB sections have content for this app and which "
        "are still empty. Use at session start to decide what to read or "
        "propose first."
    ),
    parameters=[],
    execute=_execute_kb_app_list_sections,
)


# ── propose_kb_update + commit_kb_update ─────────────────────────────────


def _make_diff(section: str, before: str, after: str) -> str:
    before_lines = (before or "").splitlines(keepends=True)
    after_lines = (after or "").splitlines(keepends=True)
    if not before_lines and not after_lines:
        return "(empty body on both sides)"
    diff_iter = difflib.unified_diff(
        before_lines, after_lines,
        fromfile=f"{section} (current)",
        tofile=f"{section} (proposed)",
        lineterm="",
    )
    text = "\n".join(line.rstrip("\n") for line in diff_iter)
    return text or "(no changes)"


async def _execute_propose_kb_update(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    section = (params.get("section") or "").strip()
    body = params.get("body") or ""
    message = (params.get("message") or "").strip()
    if not body:
        return ToolResult(success=False, error="`body` is required (non-empty)")
    err = app_kb.validate_section(section)
    if err:
        return ToolResult(success=False, error=err)
    client_code, app_code, terr = _resolve_tenant(context)
    if terr:
        return ToolResult(success=False, error=terr)

    current = await app_kb.get_latest(client_code, app_code, section)
    current_body = (current or {}).get("BODY", "") or ""
    current_version = (current or {}).get("VERSION", 0) or 0

    # Skip the diff round-trip if nothing actually changed.
    if app_kb.body_hash(body) == app_kb.body_hash(current_body):
        return ToolResult(
            success=True,
            summary=(
                f"No-op: proposed body is byte-identical to v{current_version} "
                f"of '{section}'. Nothing to commit."
            ),
        )

    pending_id = uuid.uuid4().hex
    pending = context.setdefault(_PENDING_KEY, {})
    pending[pending_id] = {
        "client_code": client_code,
        "app_code": app_code,
        "section": section,
        "body": body,
        "expected_version": current_version,
        "message": message,
    }

    is_append = section in app_kb.APPEND_ONLY_SECTIONS
    diff_block = _make_diff(section, current_body if not is_append else "", body)
    next_version = current_version + 1
    note = (
        "This section is append-only — committing adds a new entry, the "
        "current latest is shown for context only."
        if is_append else
        "Committing replaces the current latest. Prior versions are preserved "
        "in history."
    )
    summary = (
        f"Proposed update to '{section}' for {client_code}/{app_code}:\n\n"
        f"  current latest version: v{current_version}\n"
        f"  proposed new version:   v{next_version}\n"
        f"  message:                {message or '(none)'}\n"
        f"  note:                   {note}\n\n"
        f"Diff:\n```diff\n{diff_block}\n```\n\n"
        f"To commit, the user must confirm. After confirmation, call "
        f"`commit_kb_update(pending_id=\"{pending_id}\")`."
    )
    return ToolResult(success=True, summary=summary)


propose_kb_update_tool = ToolDefinition(
    name="propose_kb_update",
    description=(
        "Propose an update to one per-app KB section. Returns a unified "
        "diff vs the current latest version plus a pending_id token. The "
        "write is NOT committed until the user confirms and the agent "
        "calls `commit_kb_update(pending_id=...)`. No silent edits."
    ),
    parameters=[
        ToolParameter(name="section", type="string", enum=list(app_kb.SECTIONS), description="Section to update."),
        ToolParameter(name="body", type="string", description="Full new body for the section (replaces current latest for non-append sections; appended for decisions_log)."),
        ToolParameter(name="message", type="string", required=False, description="Commit-style note about WHY this update is happening."),
    ],
    execute=_execute_propose_kb_update,
)


async def _execute_commit_kb_update(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    pending_id = (params.get("pending_id") or "").strip()
    if not pending_id:
        return ToolResult(success=False, error="`pending_id` is required (from a prior propose_kb_update)")

    pending_map: dict[str, dict[str, Any]] = context.get(_PENDING_KEY) or {}
    pending = pending_map.get(pending_id)
    if not pending:
        return ToolResult(
            success=False,
            error=(
                f"Unknown pending_id '{pending_id}'. It may have already been "
                "committed, or the propose/commit pair was split across sessions "
                "(pending writes don't persist across session reload)."
            ),
        )

    client_code = pending["client_code"]
    app_code = pending["app_code"]
    section = pending["section"]
    body = pending["body"]
    expected_version = int(pending.get("expected_version") or 0)
    message = pending.get("message") or ""

    # Re-check current latest for optimistic-lock collision detection.
    current = await app_kb.get_latest(client_code, app_code, section)
    actual_version = (current or {}).get("VERSION", 0) or 0
    if actual_version != expected_version:
        # Drop the pending so a stale UUID doesn't linger.
        pending_map.pop(pending_id, None)
        return ToolResult(
            success=False,
            error=(
                f"Conflict: '{section}' moved from v{expected_version} to "
                f"v{actual_version} since this propose. Re-fetch with "
                f"kb_app_get and re-propose with the merged content."
            ),
        )

    updated_by = _user_id_from_context(context)
    try:
        result = await app_kb.insert_version(
            client_code, app_code, section, body,
            updated_by=updated_by, message=message,
            expected_version=expected_version,
        )
    except Exception as e:  # noqa: BLE001
        # IntegrityError on the unique (section, version) index → racing commit.
        logger.warning("insert_version failed for %s/%s/%s: %s", client_code, app_code, section, e)
        return ToolResult(
            success=False,
            error=(
                f"Commit failed (likely a concurrent write): {type(e).__name__}: {e}. "
                "Re-fetch and re-propose."
            ),
        )

    pending_map.pop(pending_id, None)
    return ToolResult(
        success=True,
        summary=(
            f"Committed v{result['version']} of '{section}' for "
            f"{client_code}/{app_code} by userId={updated_by}. "
            f"Message: {message or '(none)'}"
        ),
    )


commit_kb_update_tool = ToolDefinition(
    name="commit_kb_update",
    description=(
        "Finalize a previously-proposed KB update. Requires the pending_id "
        "returned by propose_kb_update and an explicit user confirmation in "
        "the conversation. Fails cleanly with optimistic-lock guidance if "
        "the section was modified between propose and commit."
    ),
    parameters=[
        ToolParameter(name="pending_id", type="string", description="UUID returned by the matching propose_kb_update call."),
    ],
    execute=_execute_commit_kb_update,
)


# ── Module export ────────────────────────────────────────────────────────


KB_APP_TOOLS: list[ToolDefinition] = [
    kb_app_get_tool,
    kb_app_history_tool,
    kb_app_search_tool,
    kb_app_list_sections_tool,
    propose_kb_update_tool,
    commit_kb_update_tool,
]
