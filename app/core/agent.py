"""BaseAgent — the core agentic tool-use loop.

Implements the Claude Code-style pattern:
1. Build system prompt (static docs + dynamic context)
2. Call LLM with tools
3. Stream text blocks to the client
4. For each tool_use block → execute tool → emit result
5. If stop_reason == "tool_use" → append results, loop back to step 2
6. If stop_reason == "end_turn" → done

Subclasses provide their own tools, context builder, and configuration.

Usage:
    class MyAgent(BaseAgent):
        def __init__(self):
            super().__init__(
                name="my_agent",
                tools=my_tools,
                context_builder=my_context,
            )

    agent = MyAgent()
    await agent.run(user_message, session, event_stream)
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from app.core.tools.base import ToolDefinition, ToolResult
from app.core.streaming import AgentEventStream
from app.core.session import BaseSession
from app.core.context import BaseContext
from app.services.llm_provider import get_llm_provider

logger = logging.getLogger(__name__)


class BaseAgent:
    """Base class for all agentic loops.

    Attributes:
        name: Agent identifier (for logging and tracking).
        tools: List of available tool definitions.
        context_builder: Builds the system prompt.
        model_tier: LLM model tier ("fast" or "balanced").
        max_turns: Maximum tool-use loop iterations per request.
        max_tokens: Maximum tokens per LLM response.
    """

    def __init__(
        self,
        name: str,
        tools: list[ToolDefinition],
        context_builder: BaseContext,
        model_tier: str = "balanced",
        max_turns: int = 50,
        max_tokens: int = 16384,
    ) -> None:
        self.name = name
        self.tools = {t.name: t for t in tools}
        self.context_builder = context_builder
        self.model_tier = model_tier
        self.max_turns = max_turns
        self.max_tokens = max_tokens

        # Pre-compute Anthropic tool schemas
        self._anthropic_tools = [t.to_anthropic_tool() for t in tools]

    async def run(
        self,
        user_message: str,
        session: BaseSession,
        event_stream: AgentEventStream,
    ) -> None:
        """Execute the agentic loop for a single user turn.

        This is the core method. It:
        1. Builds the system prompt
        2. Appends the user message
        3. Calls the LLM with tools
        4. Streams text, executes tools, loops until done
        5. Emits the done event

        All errors are caught and emitted as error events.

        Args:
            user_message: The user's input text.
            session: Active session with auth context and message history.
            event_stream: SSE stream to emit events to the client.
        """
        try:
            await self._run_loop(user_message, session, event_stream)
        except Exception as e:
            logger.exception(f"Agent {self.name} error")
            await event_stream.emit_error(f"Agent error: {type(e).__name__}: {e}")
            await event_stream.emit_done(
                session_id=session.session_id,
                usage=session.total_usage,
            )

    async def _run_loop(
        self,
        user_message: str,
        session: BaseSession,
        event_stream: AgentEventStream,
    ) -> None:
        """Internal agentic loop implementation."""
        provider = get_llm_provider()

        # Build system prompt
        dynamic_context = self.build_dynamic_context(session)
        system_prompt = self.context_builder.build_system_prompt(
            dynamic_context=dynamic_context,
        )

        # Append user message to conversation
        session.append_user_message(user_message)

        turn = 0
        assistant_text_parts: list[str] = []

        while turn < self.max_turns:
            turn += 1
            request_id = f"{session.session_id}_{uuid.uuid4().hex[:8]}"

            # Call LLM with tools
            start_time = time.monotonic()
            response = await provider.create_completion_with_tools(
                system_prompt=system_prompt,
                messages=session.get_messages(),
                tools=self._anthropic_tools,
                model_tier=self.model_tier,
                max_tokens=self.max_tokens,
            )
            latency_ms = int((time.monotonic() - start_time) * 1000)

            content_blocks = response["content"]
            usage = response["usage"]
            usage["latency_ms"] = latency_ms
            stop_reason = response["stop_reason"]

            # Track usage
            session.accumulate_usage(usage)
            await session.record_token_usage(usage, request_id, response["model"])

            # Process content blocks
            tool_use_blocks = []

            for block in content_blocks:
                if block["type"] == "text":
                    text = block["text"]
                    if text:
                        assistant_text_parts.append(text)
                        await event_stream.emit_text(text)

                elif block["type"] == "tool_use":
                    tool_use_blocks.append(block)

            # Save assistant message to conversation history
            session.append_assistant_message(content_blocks)

            # If no tool calls, we're done
            if stop_reason != "tool_use" or not tool_use_blocks:
                break

            # Execute tools and collect results
            tool_result_blocks = []

            for tool_block in tool_use_blocks:
                tool_name = tool_block["name"]
                tool_input = tool_block["input"]
                tool_use_id = tool_block["id"]

                await event_stream.emit_tool_start(tool_name, tool_input, tool_use_id)

                result = await self._execute_tool(tool_name, tool_input, session)

                await event_stream.emit_tool_result(
                    tool_name, result.success, result.to_tool_result_content(), tool_use_id
                )

                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": result.to_tool_result_content(),
                    "is_error": not result.success,
                })

            # Append tool results to conversation
            session.append_tool_results(tool_result_blocks)

        else:
            # Exhausted max_turns
            await event_stream.emit_text(
                f"\n\n[Reached maximum of {self.max_turns} tool-use turns. "
                "Please continue the conversation to proceed.]"
            )

        # Persist the turn summary
        assistant_summary = " ".join(assistant_text_parts)[:2000] if assistant_text_parts else ""
        await session.persist_turn(user_message, assistant_summary)

        # Emit done
        await event_stream.emit_done(
            session_id=session.session_id,
            usage=session.total_usage,
        )

    async def _execute_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        session: BaseSession,
    ) -> ToolResult:
        """Execute a single tool by name.

        Builds the context dict (auth headers, etc.) and calls the
        tool's execute function.
        """
        tool = self.tools.get(tool_name)

        if not tool:
            return ToolResult(
                success=False,
                error=f"Unknown tool: {tool_name}",
            )

        if not tool.execute:
            return ToolResult(
                success=False,
                error=f"Tool {tool_name} has no execute function",
            )

        # Build context for the tool
        context = self.build_tool_context(session)

        try:
            return await tool.execute(tool_input, context)
        except Exception as e:
            logger.exception(f"Tool {tool_name} failed")
            return ToolResult(
                success=False,
                error=f"Tool execution error: {type(e).__name__}: {e}",
            )

    def build_dynamic_context(self, session: BaseSession) -> str:
        """Build per-request dynamic context string.

        Override in subclasses to add agent-specific context
        (e.g., component catalog, app state).

        Args:
            session: Active session with auth context.

        Returns:
            Dynamic context text to append to system prompt.
        """
        if not session.auth:
            return ""
        return (
            f"Client: {session.auth.client_code}\n"
            f"App: {session.auth.app_code}\n"
        )

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        """Build context dict passed to each tool's execute function.

        Override in subclasses to add agent-specific context.

        Args:
            session: Active session with auth context.

        Returns:
            Context dict with auth headers and other metadata.
        """
        ctx: dict[str, Any] = {
            "session_id": session.session_id,
        }
        if session.auth:
            ctx["headers"] = session.auth.to_headers()
            ctx["client_code"] = session.auth.client_code
            ctx["app_code"] = session.auth.app_code
        return ctx
