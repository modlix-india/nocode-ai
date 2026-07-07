"""Built-in (server-executed) tool stream handling — web_search / web_fetch rows.

Renders server-tool results into the agent event stream as display rows.
Extracted verbatim from ``BaseAgent._run_loop``: the loop owns the per-turn
``rows`` dict (``{tool_id: {"name", "summary"}}``) and dispatches the builtin
chunk types here; these handlers emit directly on the event stream.

Anthropic emits ALL tool_use blocks first, then ALL result blocks (a batch), so
each row is tracked by ``tool_id`` rather than a single "active" slot, and every
open row is flushed at end-of-stream by ``close_builtin_rows``.

NOTE: assumes the Anthropic builtin chunk shape (``chunk.type`` /
``tool_id`` / ``tool_name`` / ``hits`` / ``text``; OpenAI ``web_search_preview``
rides the same types). Not provider-agnostic yet — an unknown ``tool_name``
falls through to the web_search formatter.
TODO: builtin streaming has no full characterization coverage yet — only the
orphaned-result crash guard (``on_builtin_tool_result``) is tested.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.core.streaming import AgentEventStream

logger = logging.getLogger(__name__)


def _compact_host(url: str) -> str:
    """Return ``host/path-tail`` compact form; empty if unparseable."""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        host = (p.netloc or "").removeprefix("www.")
        return host or url[:55]
    except Exception:
        return url[:55]


def _format_web_search_hits(hits: list[dict[str, Any]], error: str) -> str:
    """Render a pretty list of hits for a builtin web_search row.

    All hits are shown (the UI scrolls). One line per hit:
    ``N. Title — host.com``. On error, returns ``"search failed: <code>"``.
    """
    if error:
        return f"search failed: {error}"
    if not hits:
        return "no results"

    n = len(hits)
    lines = [f"Found {n} hit{'s' if n != 1 else ''}:"]
    for i, h in enumerate(hits, 1):
        title = (h.get("title") or "").strip()
        host = _compact_host(h.get("url") or "")
        if title and host:
            lines.append(f"  {i}. {title} — {host}")
        elif title:
            lines.append(f"  {i}. {title}")
        elif host:
            lines.append(f"  {i}. {host}")
    return "\n".join(lines)


def _format_web_fetch_result(hits: list[dict[str, Any]], error: str) -> str:
    """Render a one-line outcome for a builtin web_fetch row.

    ``hits`` carries a single-entry list ``[{"title", "url"}]`` on success
    (shape-compatible with web_search so the chunk-level plumbing stays
    uniform). On error, returns ``"fetch failed: <code>"``.
    """
    if error:
        return f"fetch failed: {error}"
    if not hits:
        return "no content"
    h = hits[0]
    title = (h.get("title") or "").strip()
    host = _compact_host(h.get("url") or "")
    if title and host:
        return f"fetched: {title[:80]} ({host})"
    return f"fetched: {title or host or 'page'}"


async def on_builtin_tool_use(
    rows: dict[str, dict[str, Any]], chunk: Any, event_stream: AgentEventStream,
) -> None:
    """Open (or update) the display row for a ``builtin_tool_use`` chunk.

    Rows stay open until ``close_builtin_rows`` flushes them at end-of-stream.
    """
    query = (chunk.text or "").strip()
    if not query:
        return  # empty query → don't open a blank row
    tool_id = chunk.tool_id or f"builtin_{uuid.uuid4().hex[:8]}"
    name = chunk.tool_name or "builtin_tool"
    short_q = query if len(query) <= 80 else query[:79] + "…"

    row = rows.get(tool_id)
    if row is None:
        display = name.replace("_", " ").title()
        try:
            await event_stream.emit_tool_start(
                name, {"query": short_q}, tool_id, display,
            )
        except Exception:
            pass
        row = {"name": name, "summary": ""}
        rows[tool_id] = row

    msg = f"{name} · {short_q}"
    row["summary"] = msg
    try:
        await event_stream.emit_tool_update(tool_id, msg)
    except Exception:
        pass


async def on_builtin_tool_result(
    rows: dict[str, dict[str, Any]], chunk: Any, event_stream: AgentEventStream,
) -> None:
    """Render hits/content for the builtin row with this ``tool_id``, emit the delta."""
    tool_id = chunk.tool_id
    if not tool_id:
        return
    row = rows.get(tool_id)
    if row is None:
        # Orphaned result: no paired tool_use → log + skip. WITHOUT this guard
        # the row.get() below raises AttributeError on None and kills the turn.
        logger.warning(
            "builtin_tool_result orphaned: tool=%s tool_id=%s hits=%d",
            chunk.tool_name, tool_id, len(chunk.hits),
        )
        return
    name = (chunk.tool_name or row.get("name", "") or "").lower()
    if name == "web_fetch":
        rendered = _format_web_fetch_result(chunk.hits, chunk.text)
    else:
        rendered = _format_web_search_hits(chunk.hits, chunk.text)
    # Emit ONLY the hits delta (not the cumulative summary): the UI appends each
    # update as a separate line, so re-sending the cumulative form duplicates it.
    # Keep the cumulative in row["summary"] for the end-of-stream final emit.
    row["summary"] = (
        f"{row['summary']}\n{rendered}"
        if row.get("summary") else rendered
    )
    try:
        await event_stream.emit_tool_update(tool_id, rendered)
    except Exception:
        pass


async def close_builtin_rows(
    rows: dict[str, dict[str, Any]], event_stream: AgentEventStream,
) -> None:
    """Flush the final summary for every open builtin row (end-of-stream).

    Anthropic's batch pattern (tool_use×N then result×N) leaves multiple rows
    open at once; each gets its final ``emit_tool_result`` here. MUST be called
    after the stream loop. NOT guarded on exception/cancel today — if the stream
    loop raises mid-turn, open rows won't be closed (deferred: a try/finally
    guard at the call site).
    """
    for _tid, _row in rows.items():
        try:
            await event_stream.emit_tool_result(
                _row.get("name", "builtin_tool"),
                True,
                _row.get("summary", ""),
                _tid,
            )
        except Exception:
            pass
