"""ResultStore — server-side persistence for oversized tool results.

When a tool result exceeds the tiered character limit it is stored here
and a compact reference is returned to the LLM.  The LLM can then page
through the full content with the ``read_result`` deferred tool.

Design:
    - In-memory dict (no external dependency).
    - TTL: 30 minutes (entries auto-evict on read).
    - Max 100 entries per store instance (one per session).
    - Thread-safe via asyncio (single-threaded event loop).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from app.core.tools.base import (
    ToolDefinition,
    ToolParameter,
    ToolResult,
    ResultTier,
)


_DEFAULT_TTL_SECONDS = 30 * 60  # 30 minutes
_MAX_ENTRIES = 100
_DEFAULT_PAGE_SIZE = 4000  # chars per page


class ResultStore:
    """In-memory store for oversized tool results."""

    def __init__(self, ttl: int = _DEFAULT_TTL_SECONDS, max_entries: int = _MAX_ENTRIES):
        self._entries: dict[str, tuple[str, float]] = {}  # id → (content, created_at)
        self._ttl = ttl
        self._max_entries = max_entries

    def store(self, content: str) -> str:
        """Store content and return a unique result_id."""
        self._evict_expired()

        # If at capacity, evict oldest
        while len(self._entries) >= self._max_entries:
            oldest_id = min(self._entries, key=lambda k: self._entries[k][1])
            del self._entries[oldest_id]

        result_id = uuid.uuid4().hex[:12]
        self._entries[result_id] = (content, time.monotonic())
        return result_id

    def read(self, result_id: str, offset: int = 0, limit: int = _DEFAULT_PAGE_SIZE) -> str | None:
        """Read a section of stored content. Returns None if not found/expired."""
        self._evict_expired()
        entry = self._entries.get(result_id)
        if entry is None:
            return None
        content, _ = entry
        return content[offset:offset + limit]

    def total_length(self, result_id: str) -> int | None:
        """Return total character count for a stored result, or None if not found."""
        entry = self._entries.get(result_id)
        if entry is None:
            return None
        return len(entry[0])

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [k for k, (_, ts) in self._entries.items() if now - ts > self._ttl]
        for k in expired:
            del self._entries[k]


# ── read_result tool ─────────────────────────────────────────────


async def _execute_read_result(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    """Read a section of a previously stored large result."""
    result_id = params.get("result_id", "")
    offset = params.get("offset", 0)
    limit = params.get("limit", _DEFAULT_PAGE_SIZE)

    if not result_id:
        return ToolResult(success=False, error="result_id is required.")

    store: ResultStore | None = context.get("result_store")
    if store is None:
        return ToolResult(success=False, error="No result store available in this session.")

    content = store.read(result_id, offset, limit)
    if content is None:
        return ToolResult(
            success=False,
            error=f"Result '{result_id}' not found or expired (TTL 30 minutes).",
        )

    total = store.total_length(result_id) or 0
    end = offset + len(content)
    has_more = end < total

    return ToolResult(
        success=True,
        summary=(
            f"[chars {offset}-{end} of {total}]\n\n"
            f"{content}"
            + (f"\n\n... [more available — use offset={end} to continue]" if has_more else "")
        ),
        result_tier=ResultTier.STANDARD,
    )


READ_RESULT_TOOL = ToolDefinition(
    name="read_result",
    display_name="Read Result",
    description=(
        "Read a section of a previously stored large result. "
        "Use when a tool result was truncated and a result_id was provided."
    ),
    parameters=[
        ToolParameter(
            name="result_id",
            type="string",
            description="The result_id from a truncated tool result.",
            required=True,
        ),
        ToolParameter(
            name="offset",
            type="integer",
            description="Character offset to start reading from (default 0).",
            required=False,
            default=0,
        ),
        ToolParameter(
            name="limit",
            type="integer",
            description="Maximum characters to return (default 4000).",
            required=False,
            default=_DEFAULT_PAGE_SIZE,
        ),
    ],
    execute=_execute_read_result,
    is_deferred=True,
    search_hint="read large result page offset continue truncated",
    result_tier=ResultTier.STANDARD,
)
