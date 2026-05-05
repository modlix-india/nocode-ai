"""Planning tools — create, track, and manage structured build plans.

When the agent receives a complex task (3+ pages or 3+ entity types),
it should create a plan first, then execute steps sequentially or
delegate them to sub-agents.

Plans are stored in ``session.context["plan"]`` and survive compaction
(re-injected as priority state by the compaction engine).
"""

from __future__ import annotations

import json
import time
from typing import Any

from app.core.tools.base import (
    ToolDefinition,
    ToolParameter,
    ToolResult,
    ResultTier,
)


# ── Plan data model ──────────────────────────────────────────────

def _new_plan(goal: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Create a new plan structure."""
    enriched_steps = []
    for i, step in enumerate(steps):
        enriched_steps.append({
            "id": step.get("id", i + 1),
            "task": step["task"],
            "status": "pending",
            "deps": step.get("deps", []),
            "notes": "",
        })
    return {
        "goal": goal,
        "steps": enriched_steps,
        "created_at": time.time(),
        "updated_at": time.time(),
    }


def _format_plan(plan: dict[str, Any]) -> str:
    """Format a plan as a readable summary."""
    lines = [f"## Plan: {plan['goal']}", ""]

    steps = plan.get("steps", [])
    done = sum(1 for s in steps if s["status"] == "completed")
    in_progress = sum(1 for s in steps if s["status"] == "in_progress")
    pending = sum(1 for s in steps if s["status"] == "pending")

    lines.append(f"Progress: {done}/{len(steps)} done, {in_progress} in progress, {pending} pending")
    lines.append("")

    status_icons = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]", "failed": "[!]", "skipped": "[-]"}

    for step in steps:
        icon = status_icons.get(step["status"], "[?]")
        deps_str = f" (after: {step['deps']})" if step.get("deps") else ""
        notes_str = f" — {step['notes']}" if step.get("notes") else ""
        lines.append(f"  {icon} Step {step['id']}: {step['task']}{deps_str}{notes_str}")

    return "\n".join(lines)


# ── create_plan ──────────────────────────────────────────────────


async def _execute_create_plan(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    """Create a structured build plan."""
    goal = params.get("goal", "")
    steps = params.get("steps", [])

    if not goal:
        return ToolResult(success=False, error="goal is required.")
    if not steps:
        return ToolResult(success=False, error="steps array is required (at least 1 step).")

    for i, step in enumerate(steps):
        if not step.get("task"):
            return ToolResult(success=False, error=f"steps[{i}] missing 'task' field.")

    plan = _new_plan(goal, steps)

    session_ctx = context.get("session_context")
    if session_ctx is not None:
        session_ctx["plan"] = plan

    return ToolResult(
        success=True,
        summary=_format_plan(plan),
        result_tier=ResultTier.COMPACT,
    )


CREATE_PLAN = ToolDefinition(
    name="create_plan",
    description=(
        "Create a structured build plan with dependency tracking. "
        "Use for tasks involving 3+ pages or 3+ entity types. "
        "Each step has a task description and optional dependencies on other step IDs."
    ),
    parameters=[
        ToolParameter(
            name="goal",
            type="string",
            description="High-level goal (e.g. 'Build CRM with contacts, deals, dashboard').",
            required=True,
        ),
        ToolParameter(
            name="steps",
            type="array",
            description=(
                "Array of steps. Each: {task: str, deps?: int[]}. "
                "Example: [{task: 'Create application'}, {task: 'Create theme', deps: [1]}, "
                "{task: 'Build contacts page', deps: [2]}]"
            ),
            required=True,
            items={"type": "object"},
        ),
    ],
    execute=_execute_create_plan,
    is_deferred=True,
    search_hint="plan decompose steps tasks dependencies goal",
    result_tier=ResultTier.COMPACT,
)


# ── update_plan ──────────────────────────────────────────────────


async def _execute_update_plan(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    """Update a plan step's status and notes."""
    step_id = params.get("step_id")
    status = params.get("status", "")
    notes = params.get("notes", "")

    if step_id is None:
        return ToolResult(success=False, error="step_id is required.")

    session_ctx = context.get("session_context")
    if session_ctx is None or "plan" not in session_ctx:
        return ToolResult(success=False, error="No plan exists. Create one with create_plan first.")

    plan = session_ctx["plan"]
    step = None
    for s in plan.get("steps", []):
        if s["id"] == step_id:
            step = s
            break

    if step is None:
        ids = [s["id"] for s in plan.get("steps", [])]
        return ToolResult(success=False, error=f"Step {step_id} not found. Available: {ids}")

    valid_statuses = {"pending", "in_progress", "completed", "failed", "skipped"}
    if status and status not in valid_statuses:
        return ToolResult(success=False, error=f"Invalid status '{status}'. Valid: {valid_statuses}")

    if status:
        step["status"] = status
    if notes:
        step["notes"] = notes
    plan["updated_at"] = time.time()

    return ToolResult(
        success=True,
        summary=_format_plan(plan),
        result_tier=ResultTier.COMPACT,
    )


UPDATE_PLAN = ToolDefinition(
    name="update_plan",
    description="Update a plan step's status (pending/in_progress/completed/failed/skipped) and optional notes.",
    parameters=[
        ToolParameter(name="step_id", type="integer", description="Step ID to update.", required=True),
        ToolParameter(
            name="status",
            type="string",
            description="New status.",
            required=False,
            enum=["pending", "in_progress", "completed", "failed", "skipped"],
        ),
        ToolParameter(name="notes", type="string", description="Optional notes about this step.", required=False),
    ],
    execute=_execute_update_plan,
    is_deferred=True,
    search_hint="mark step done progress status update plan",
    result_tier=ResultTier.COMPACT,
)


# ── get_plan ─────────────────────────────────────────────────────


async def _execute_get_plan(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    """Retrieve the current plan and progress."""
    session_ctx = context.get("session_context")
    if session_ctx is None or "plan" not in session_ctx:
        return ToolResult(success=True, summary="No plan exists.", result_tier=ResultTier.COMPACT)

    plan = session_ctx["plan"]
    return ToolResult(
        success=True,
        summary=_format_plan(plan),
        result_tier=ResultTier.COMPACT,
    )


GET_PLAN = ToolDefinition(
    name="get_plan",
    description="Retrieve the current build plan and progress status.",
    parameters=[],
    execute=_execute_get_plan,
    is_deferred=True,
    search_hint="retrieve plan status progress overview",
    result_tier=ResultTier.COMPACT,
)


# ── Exports ──────────────────────────────────────────────────────

PLANNING_TOOLS: list[ToolDefinition] = [
    CREATE_PLAN,
    UPDATE_PLAN,
    GET_PLAN,
]
