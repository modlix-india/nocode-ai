"""Deferred-tool meta-surface: search_tools + get_tool_schema.

Pattern mirrors Claude Code's own ToolSearch model. With 195+ ported tools
plus the kb_app / code_workspace tools, full inline schemas would burn
~200K tokens/turn. Instead:
  1. The system prompt lists tool names + 1-line summaries (~4K tokens).
  2. The LLM calls `search_tools("query")` when it needs to discover.
  3. The LLM calls `get_tool_schema("name")` to pull the full schema.
  4. The agent loop caches fetched schema names in
     `session.context["fetched_schemas"]: set[str]` so a tool used many
     times doesn't keep re-fetching.

This module only provides the two meta-tools. The agent-loop change that
auto-injects the schema when an LLM calls a tool BEFORE fetching it (the
synthetic-schema-response pattern) is a separate edit to
`app/core/agent.py`, deferred to a follow-up session because it touches the
hot dispatch path.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult


_MAX_SEARCH_RESULTS = 25  # absolute ceiling regardless of caller's request


def _build_catalog(ctx_tools: list[ToolDefinition] | None) -> dict[str, ToolDefinition]:
    """Return the {name → ToolDefinition} map the meta-tools search over.

    Falls back to the legacy registry's `ALL_TOOLS` if the agent didn't
    provide an explicit list via context. Keeping the lookup lazy means
    these meta-tools work both during the transition (when ALL_TOOLS is
    the canonical surface) AND after Phase 1.4b retires legacy CRUD in
    favor of MODLIX_TOOLS.
    """
    if ctx_tools:
        return {t.name: t for t in ctx_tools}
    # Lazy import to avoid cycle on package init.
    from app.agents.appbuilder.tools.registry import ALL_TOOLS  # noqa: PLC0415
    return {t.name: t for t in ALL_TOOLS}


def _one_line_summary(t: ToolDefinition) -> str:
    """Extract the first non-empty line of description for compact display."""
    desc = (t.description or "").strip()
    if not desc:
        return ""
    first = desc.split("\n", 1)[0].strip()
    # Cap at ~120 chars so search-result blocks stay readable.
    return first if len(first) <= 120 else first[:117] + "..."


def _match_score(query: str, name: str, summary: str) -> int:
    """Cheap rank: name-prefix > name-substring > summary-substring > nothing.

    Returns a score 0..100; higher is more relevant. Used to sort matches
    when many tools share keywords. Substring-only search, no fuzzy match
    (the LLM has the full name list in its system prompt anyway, so this
    is for casting a wider net).
    """
    q = query.lower().strip()
    if not q:
        return 0
    n = name.lower()
    s = summary.lower()
    if n == q:
        return 100
    if n.startswith(q):
        return 90
    if q in n:
        return 70
    if q in s:
        return 40
    return 0


# ── search_tools ──────────────────────────────────────────────────────────


async def _execute_search_tools(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    query = (params.get("query") or "").strip()
    if not query:
        return ToolResult(success=False, error="`query` is required (non-empty string)")
    try:
        max_results = int(params.get("max_results") or 8)
    except (TypeError, ValueError):
        max_results = 8
    max_results = max(1, min(max_results, _MAX_SEARCH_RESULTS))

    catalog = _build_catalog(context.get("tools"))
    scored: list[tuple[int, str, str]] = []
    for name, t in catalog.items():
        summary = _one_line_summary(t)
        score = _match_score(query, name, summary)
        if score > 0:
            scored.append((score, name, summary))
    scored.sort(key=lambda r: (-r[0], r[1]))

    rows = scored[:max_results]
    if not rows:
        return ToolResult(
            success=True,
            summary=(
                f"No tools matched '{query}'. Try a different keyword or check "
                "the system prompt's tool index. The catalog has "
                f"{len(catalog)} tools total."
            ),
        )
    body_lines = [
        f"Matched {len(rows)} of {len(scored)} tool(s) for '{query}':",
        "",
    ]
    for _score, name, summary in rows:
        body_lines.append(f"- **{name}** — {summary}")
    body_lines.extend([
        "",
        "Call `get_tool_schema(name=\"<tool_name>\")` for the full parameter "
        "schema before invoking a tool you haven't used this session.",
    ])
    return ToolResult(success=True, summary="\n".join(body_lines))


search_tools_tool = ToolDefinition(
    name="search_tools",
    description=(
        "Search the full tool catalog by keyword. Returns names + one-line "
        "summaries ranked by relevance. Use when you need a capability and "
        "aren't sure which tool offers it. The catalog has ~200 tools; the "
        "system prompt lists their names + one-liners — this tool is for "
        "discovering by keyword instead of scanning the index."
    ),
    parameters=[
        ToolParameter(
            name="query", type="string",
            description="Keyword(s) to search across tool names and summaries (case-insensitive substring).",
        ),
        ToolParameter(
            name="max_results", type="integer", required=False, default=8,
            description="How many top matches to return. Capped at 25.",
        ),
    ],
    execute=_execute_search_tools,
)


# ── get_tool_schema ───────────────────────────────────────────────────────


async def _execute_get_tool_schema(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")

    catalog = _build_catalog(context.get("tools"))
    tool = catalog.get(name)
    if tool is None:
        # Cheap suggestion: any tool whose name contains the query
        suggestions = [n for n in catalog if name.lower() in n.lower()][:5]
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        return ToolResult(
            success=False,
            error=f"Unknown tool '{name}'.{hint} Use `search_tools` to discover by keyword.",
        )

    # Mark the schema as fetched on this session so the agent loop's
    # synthetic-injection pass (when added) knows not to short-circuit
    # subsequent invocations.
    fetched = context.setdefault("fetched_schemas", set())
    if isinstance(fetched, set):
        fetched.add(name)

    anthropic_shape = tool.to_anthropic_tool()
    payload = {
        "name": tool.name,
        "description": tool.description,
        "input_schema": anthropic_shape.get("input_schema", {"type": "object", "properties": {}}),
    }
    return ToolResult(
        success=True,
        summary=(
            f"Schema for **{name}**:\n```json\n"
            + json.dumps(payload, indent=2, default=str)
            + "\n```"
        ),
    )


get_tool_schema_tool = ToolDefinition(
    name="get_tool_schema",
    description=(
        "Return the full JSON schema (description + parameters) for a tool "
        "by name. Call this once per tool you plan to invoke this session — "
        "the schema is cached in the session context. Use after "
        "`search_tools` finds a candidate, or directly when you know the "
        "exact tool name from the system prompt's index."
    ),
    parameters=[
        ToolParameter(
            name="name", type="string",
            description="Exact tool name (e.g. 'add_component', 'screenshot_page').",
        ),
    ],
    execute=_execute_get_tool_schema,
)


# ── Module export ────────────────────────────────────────────────────────


META_TOOLS: list[ToolDefinition] = [
    search_tools_tool,
    get_tool_schema_tool,
]
