"""ToolSearchTool — discovers deferred tools by keyword or direct name selection.

Mirrors Claude Code's deferred tool loading pattern.  Only core tools are
included in the initial prompt; the LLM calls this tool to discover and
load additional tools on demand.

Query formats:
  - Keyword search: "theme color breakpoint"  (best-effort fuzzy match)
  - Direct selection: "select:create_theme,update_theme"  (exact names)

Discovered tool names are tracked in ``session.context["discovered_tools"]``
so the agent loop can include their schemas in subsequent LLM calls.
"""

from __future__ import annotations

import re
from typing import Any

from app.core.tools.base import (
    ToolDefinition,
    ToolParameter,
    ToolResult,
    ResultTier,
)


# ── Scoring weights ──────────────────────────────────────────────

_WEIGHT_EXACT_NAME = 10
_WEIGHT_PARTIAL_NAME = 5
_WEIGHT_SEARCH_HINT = 4
_WEIGHT_DESCRIPTION = 2

_DEFAULT_MAX_RESULTS = 5


# ── Search logic ─────────────────────────────────────────────────


def _score_tool(tool: ToolDefinition, keywords: list[str]) -> int:
    """Score a deferred tool against a set of search keywords."""
    score = 0
    name_lower = tool.name.lower()
    hint_lower = tool.search_hint.lower() if tool.search_hint else ""
    desc_lower = tool.description.lower()

    for kw in keywords:
        kw = kw.lower().strip()
        if not kw:
            continue

        # Exact name match
        if kw == name_lower:
            score += _WEIGHT_EXACT_NAME
            continue

        # Partial name match (keyword appears within the tool name)
        if kw in name_lower:
            score += _WEIGHT_PARTIAL_NAME
            continue

        # search_hint match
        if hint_lower and kw in hint_lower:
            score += _WEIGHT_SEARCH_HINT
            continue

        # Word-boundary match in description
        if re.search(rf"\b{re.escape(kw)}\b", desc_lower):
            score += _WEIGHT_DESCRIPTION

    return score


def search_tools(
    query: str,
    deferred_tools: list[ToolDefinition],
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> list[ToolDefinition]:
    """Search deferred tools by keyword or direct selection.

    Returns up to *max_results* matching tools, sorted by relevance score.
    """
    query = query.strip()
    if not query:
        return []

    # Direct selection: "select:tool_name1,tool_name2"
    if query.lower().startswith("select:"):
        names = {n.strip().lower() for n in query[7:].split(",") if n.strip()}
        return [t for t in deferred_tools if t.name.lower() in names]

    # Keyword search
    keywords = query.split()
    scored: list[tuple[int, ToolDefinition]] = []
    for tool in deferred_tools:
        s = _score_tool(tool, keywords)
        if s > 0:
            scored.append((s, tool))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [t for _, t in scored[:max_results]]


def format_tool_summary(tool: ToolDefinition) -> str:
    """Format a discovered tool as a compact summary for the LLM."""
    params = ", ".join(
        f"{p.name}: {p.type}{'?' if not p.required else ''}"
        for p in tool.parameters
    )
    return f"- **{tool.name}**({params}): {tool.description}"


# ── Tool execute function ────────────────────────────────────────


async def _execute_tool_search(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    """Execute a tool search query against deferred tools."""
    query = params.get("query", "").strip()
    max_results = params.get("max_results", _DEFAULT_MAX_RESULTS)

    if not query:
        return ToolResult(
            success=False,
            error="query is required. Use keywords (e.g. 'theme color') or 'select:tool_name'.",
        )

    deferred_tools: list[ToolDefinition] = context.get("deferred_tools", [])
    if not deferred_tools:
        return ToolResult(
            success=True,
            summary="No deferred tools available.",
            result_tier=ResultTier.COMPACT,
        )

    matches = search_tools(query, deferred_tools, max_results=max_results)

    if not matches:
        # Provide the full list of available tool names as a hint
        all_names = [t.name for t in deferred_tools]
        return ToolResult(
            success=True,
            summary=(
                f"No tools matched '{query}'.\n\n"
                f"Available deferred tools: {', '.join(all_names)}"
            ),
            result_tier=ResultTier.COMPACT,
        )

    # Track discovered tools in session context
    session_ctx = context.get("session_context")
    if session_ctx is not None:
        discovered = session_ctx.setdefault("discovered_tools", [])
        for tool in matches:
            if tool.name not in discovered:
                discovered.append(tool.name)

    # Format results
    summaries = [format_tool_summary(t) for t in matches]
    return ToolResult(
        success=True,
        data={"discovered": [t.name for t in matches]},
        summary=(
            f"Found {len(matches)} tool(s):\n\n"
            + "\n".join(summaries)
            + "\n\nThese tools are now available for use."
        ),
        result_tier=ResultTier.COMPACT,
    )


# ── Tool definition ──────────────────────────────────────────────

TOOL_SEARCH = ToolDefinition(
    name="tool_search",
    display_name="Tool Search",
    description=(
        "Discover additional tools by keyword or name. "
        "Use keywords like 'theme color' or 'version history' to find relevant tools. "
        "Use 'select:tool_name' for direct selection. "
        "Discovered tools become available for use in subsequent calls."
    ),
    parameters=[
        ToolParameter(
            name="query",
            type="string",
            description=(
                "Search query. Keywords (e.g. 'theme color breakpoint') for fuzzy match, "
                "or 'select:name1,name2' for direct selection."
            ),
            required=True,
        ),
        ToolParameter(
            name="max_results",
            type="integer",
            description="Maximum number of tools to return (default 5).",
            required=False,
            default=5,
        ),
    ],
    execute=_execute_tool_search,
    is_deferred=False,  # ToolSearch itself is NEVER deferred
    search_hint="",
    result_tier=ResultTier.COMPACT,
)
