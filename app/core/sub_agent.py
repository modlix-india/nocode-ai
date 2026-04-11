"""Sub-agent infrastructure — spawns isolated agent instances for subtasks.

A sub-agent runs with its own message history (fresh context window) but
shares the parent's auth context, tools, definition cache, and component
catalog.  This allows the orchestrator to delegate independent page builds
or feature implementations without exhausting the main context.

Design:
    - Fresh context window per sub-agent (no inherited messages)
    - Shared auth, tools, definition cache, catalog (read-through)
    - Own compaction lifecycle (if subtask is long)
    - Returns a structured SubAgentResult to the parent
    - Sequential execution by default; parallel via asyncio.gather()

Usage:
    sub = SubAgent(
        parent_session=session,
        task="Build the contacts page with a table and CRUD forms",
        context_summary="App code is mycrm, theme uses primaryColor=#3B82F6",
        tools=all_tools,
    )
    result = await sub.run(provider, event_stream)
    # result.success, result.summary, result.entities_created
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.tools.base import ToolDefinition, ToolResult
from app.core.session import BaseSession, AuthContext
from app.core.streaming import AgentEventStream
from app.core.compaction import ContextCompactor

logger = logging.getLogger(__name__)


@dataclass
class SubAgentResult:
    """Structured result from a sub-agent execution."""

    success: bool
    summary: str
    entities_created: list[str] = field(default_factory=list)
    entities_modified: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    turn_count: int = 0
    duration_ms: int = 0


class SubSession(BaseSession):
    """Lightweight session for sub-agents with isolated message history.

    Shares auth and context with the parent but starts with an empty
    message history and its own turn counter.
    """

    def __init__(self, parent: BaseSession, task_context: str) -> None:
        super().__init__(agent_name=parent.agent_name + "_sub")
        self.session_id = f"{parent.session_id}_sub_{uuid.uuid4().hex[:6]}"
        self.auth = parent.auth

        # Share parent's context (app_code, discovered_tools, etc.)
        # but don't share messages
        self.context = dict(parent.context)
        self.context.pop("plan", None)  # Sub-agent doesn't own the plan

        # Inject the task context as the initial system knowledge
        self._task_context = task_context

    async def get_or_create(self, session_id=None, auth=None) -> str:
        # No-op — sub-sessions don't persist to DB
        return self.session_id

    async def persist_turn(self, *args, **kwargs) -> None:
        pass  # Sub-agent turns are not persisted individually

    async def persist_turn_incremental(self, *args, **kwargs) -> None:
        pass

    async def save_context(self) -> None:
        pass

    async def complete(self) -> None:
        pass

    async def set_processing(self) -> None:
        pass

    async def record_token_usage(self, *args, **kwargs) -> None:
        pass  # Token usage tracked at parent level


class SubAgent:
    """An isolated agent instance spawned for a specific subtask."""

    def __init__(
        self,
        parent_session: BaseSession,
        task: str,
        context_summary: str,
        tools: dict[str, ToolDefinition],
        context_builder: Any,
        model_tier: str = "balanced",
        max_turns: int = 30,
        max_tokens: int = 8192,
    ) -> None:
        self.task = task
        self.context_summary = context_summary
        self.tools = tools
        self.context_builder = context_builder
        self.model_tier = model_tier
        self.max_turns = max_turns
        self.max_tokens = max_tokens
        self.subtask_id = uuid.uuid4().hex[:8]

        self._session = SubSession(parent_session, context_summary)
        self._compactor = ContextCompactor(
            context_limit=180_000,
            threshold=0.85,
            keep_recent=2,
        )

    async def run(
        self,
        provider: Any,
        event_stream: AgentEventStream,
        tool_context_builder: Any = None,
    ) -> SubAgentResult:
        """Execute the subtask and return a structured result.

        Args:
            provider: LLM provider for API calls.
            event_stream: SSE stream (shared with parent for progress events).
            tool_context_builder: Callable(session) -> dict for building tool context.
        """
        start_time = time.monotonic()

        await event_stream.emit_subtask_start(
            self.subtask_id, self.task,
        )

        # Inject the task as the first user message
        task_message = (
            f"You are a sub-agent working on a specific task. "
            f"Complete this task and report what you created/modified.\n\n"
            f"## Context\n{self.context_summary}\n\n"
            f"## Task\n{self.task}"
        )
        self._session.append_user_message(task_message)

        # Build system prompt
        dynamic_context = ""
        if hasattr(self.context_builder, 'build_system_prompt'):
            dynamic_context = self.context_builder.build_system_prompt(dynamic_context="")
        system_prompt = dynamic_context or "You are an application builder sub-agent."

        # Build tool schemas
        anthropic_tools = [t.to_anthropic_tool() for t in self.tools.values()]

        entities_created: list[str] = []
        entities_modified: list[str] = []
        errors: list[str] = []
        text_parts: list[str] = []
        turn = 0

        try:
            while turn < self.max_turns:
                if event_stream.is_cancelled:
                    break

                turn += 1

                response = await provider.create_completion_with_tools(
                    system_prompt=system_prompt,
                    messages=self._session.get_messages(),
                    tools=anthropic_tools,
                    model_tier=self.model_tier,
                    max_tokens=self.max_tokens,
                )

                content_blocks = response["content"]
                stop_reason = response["stop_reason"]

                # Collect text
                for block in content_blocks:
                    if block.get("type") == "text" and block.get("text"):
                        text_parts.append(block["text"])

                self._session.append_assistant_message(content_blocks)

                if stop_reason != "tool_use":
                    break

                # Execute tools
                tool_use_blocks = [b for b in content_blocks if b.get("type") == "tool_use"]
                tool_result_blocks = []

                for tool_block in tool_use_blocks:
                    if event_stream.is_cancelled:
                        break

                    tool_name = tool_block["name"]
                    tool_input = tool_block["input"]
                    tool_use_id = tool_block["id"]

                    tool = self.tools.get(tool_name)
                    if not tool or not tool.execute:
                        result = ToolResult(success=False, error=f"Unknown tool: {tool_name}")
                    else:
                        ctx = tool_context_builder(self._session) if tool_context_builder else {}
                        try:
                            result = await tool.execute(tool_input, ctx)
                        except Exception as e:
                            result = ToolResult(success=False, error=str(e))
                            errors.append(f"{tool_name}: {e}")

                    # Track entity changes
                    if result.success and tool_name in ("create", "create_entity"):
                        name = tool_input.get("name", "?")
                        entities_created.append(f"{tool_input.get('object_type', '?')}/{name}")
                    elif result.success and tool_name in ("update", "update_entity", "patch_components", "patch_event"):
                        name = tool_input.get("page_name") or tool_input.get("name", "?")
                        entities_modified.append(f"{tool_input.get('object_type', 'page')}/{name}")

                    tool_result_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": result.to_tool_result_content(),
                        "is_error": not result.success,
                    })

                self._session.append_tool_results(tool_result_blocks)

                # Check compaction
                if self._compactor.should_compact(self._session):
                    await self._compactor.compact(self._session, provider)

        except Exception as e:
            logger.exception("Sub-agent %s failed", self.subtask_id)
            errors.append(str(e))

        duration_ms = int((time.monotonic() - start_time) * 1000)
        summary = " ".join(text_parts[-3:]) if text_parts else "Subtask completed."
        if len(summary) > 500:
            summary = summary[:500] + "..."

        success = len(errors) == 0
        result = SubAgentResult(
            success=success,
            summary=summary,
            entities_created=entities_created,
            entities_modified=entities_modified,
            errors=errors,
            turn_count=turn,
            duration_ms=duration_ms,
        )

        await event_stream.emit_subtask_done(
            self.subtask_id, success, result.summary,
        )

        return result


async def run_subtasks_parallel(
    subtasks: list[SubAgent],
    provider: Any,
    event_stream: AgentEventStream,
    tool_context_builder: Any = None,
) -> list[SubAgentResult]:
    """Run multiple sub-agents concurrently.

    Independent plan steps (e.g., building 3 different pages) can run
    in parallel.  The planner must ensure no two parallel steps target
    the same object.
    """
    coros = [
        sub.run(provider, event_stream, tool_context_builder)
        for sub in subtasks
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)

    final: list[SubAgentResult] = []
    for r in results:
        if isinstance(r, Exception):
            final.append(SubAgentResult(
                success=False,
                summary=f"Sub-agent failed: {r}",
                errors=[str(r)],
            ))
        else:
            final.append(r)
    return final
