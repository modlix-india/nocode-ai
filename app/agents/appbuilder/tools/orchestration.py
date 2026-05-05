"""Orchestration tool — delegates subtasks to sub-agents.

The orchestrator agent calls ``delegate_task`` to spawn a sub-agent with
a fresh context window for an isolated piece of work (e.g., building a
single page).  The sub-agent executes independently and returns a
structured result summary.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.tools.base import (
    ToolDefinition,
    ToolParameter,
    ToolResult,
    ResultTier,
)


async def _execute_delegate_task(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    """Spawn a sub-agent for an isolated subtask."""
    task = params.get("task", "")
    context_summary = params.get("context_summary", "")

    if not task:
        return ToolResult(success=False, error="task description is required.")

    # These are injected by the orchestrator agent's build_tool_context
    session = context.get("_session")
    provider = context.get("_provider")
    event_stream = context.get("_event_stream")
    all_tools = context.get("_all_tools")
    context_builder = context.get("_context_builder")
    tool_context_builder = context.get("_tool_context_builder")

    if not all(v is not None for v in [session, provider, event_stream, all_tools]):
        return ToolResult(
            success=False,
            error=(
                "Sub-agent delegation requires orchestrator context "
                "(_session, _provider, _event_stream, _all_tools). "
                "This tool can only be used during orchestrated execution."
            ),
        )

    from app.core.sub_agent import SubAgent

    sub = SubAgent(
        parent_session=session,
        task=task,
        context_summary=context_summary,
        tools=all_tools,
        context_builder=context_builder,
    )

    result = await sub.run(provider, event_stream, tool_context_builder)

    # Format the result for the orchestrator
    parts = [f"Subtask completed ({'success' if result.success else 'with errors'})."]
    parts.append(f"Summary: {result.summary}")

    if result.entities_created:
        parts.append(f"Created: {', '.join(result.entities_created)}")
    if result.entities_modified:
        parts.append(f"Modified: {', '.join(result.entities_modified)}")
    if result.errors:
        parts.append(f"Errors: {'; '.join(result.errors)}")

    parts.append(f"Turns: {result.turn_count}, Duration: {result.duration_ms}ms")

    return ToolResult(
        success=result.success,
        data={
            "entities_created": result.entities_created,
            "entities_modified": result.entities_modified,
            "errors": result.errors,
            "turn_count": result.turn_count,
        },
        summary="\n".join(parts),
        result_tier=ResultTier.STANDARD,
    )


DELEGATE_TASK = ToolDefinition(
    name="delegate_task",
    description=(
        "Delegate a subtask to a sub-agent with a fresh context window. "
        "Use for independent work like building a single page or implementing "
        "a feature. The sub-agent shares your auth and tools but has its own "
        "message history. Returns a summary of what was created/modified."
    ),
    parameters=[
        ToolParameter(
            name="task",
            type="string",
            description=(
                "Detailed task description. Be specific about what to build, "
                "what components to include, what styling to apply, etc."
            ),
            required=True,
        ),
        ToolParameter(
            name="context_summary",
            type="string",
            description=(
                "Context the sub-agent needs: app_code, theme colors, "
                "page naming conventions, design patterns established so far, etc."
            ),
            required=True,
        ),
    ],
    execute=_execute_delegate_task,
    is_deferred=True,
    search_hint="delegate spawn sub-agent subtask parallel independent",
    result_tier=ResultTier.STANDARD,
)

ORCHESTRATION_TOOLS: list[ToolDefinition] = [DELEGATE_TASK]
