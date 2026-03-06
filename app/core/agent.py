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

import asyncio
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
        provider: str | None = None,
    ) -> None:
        self.name = name
        self.tools = {t.name: t for t in tools}
        self.context_builder = context_builder
        self.model_tier = model_tier
        self.max_turns = max_turns
        self.max_tokens = max_tokens
        self._provider_name = provider

        # Pre-compute Anthropic tool schemas
        self._anthropic_tools = [t.to_anthropic_tool() for t in tools]

        # Hold references to background tasks to prevent premature GC
        self._background_tasks: set[asyncio.Task] = set()

    async def run(
        self,
        user_message: str,
        session: BaseSession,
        event_stream: AgentEventStream,
        image_blocks: list[dict[str, Any]] | None = None,
        model_override: str | None = None,
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
            image_blocks: Optional image content blocks (Anthropic format) to include with the message.
            model_override: Optional model ID in "provider:model" format to override the default.
        """
        try:
            logger.info("Agent '%s' run: session=%s, provider=%s, model_override=%s, images=%s",
                       self.name, session.session_id, self._provider_name or "(default)",
                       model_override or "(none)",
                       len(image_blocks) if image_blocks else 0)
            await self._run_loop(user_message, session, event_stream, image_blocks, model_override)
        except Exception as e:
            logger.exception("Agent '%s' error in session %s", self.name, session.session_id)
            error_text = f"Agent error: {type(e).__name__}: {e}"
            await event_stream.emit_error(error_text)
            # Persist the error as a turn so it shows in session history
            await session.persist_turn(
                user_message, error_text, None
            )
            await session.complete()
            await event_stream.emit_done(
                session_id=session.session_id,
                usage=session.total_usage,
            )

    async def _run_loop(
        self,
        user_message: str,
        session: BaseSession,
        event_stream: AgentEventStream,
        image_blocks: list[dict[str, Any]] | None = None,
        model_override: str | None = None,
    ) -> None:
        """Internal agentic loop implementation."""
        # Resolve provider and model: use override if specified, else defaults
        override_model: str | None = None
        if model_override:
            from app.services.llm_provider import resolve_model_override
            override_provider, override_model = resolve_model_override(model_override)
            provider = get_llm_provider(override_provider)
            logger.info("Model override: provider=%s, model=%s", override_provider, override_model)
        else:
            provider = get_llm_provider(self._provider_name)
        logger.info("Provider resolved: %s (class=%s)", self._provider_name, type(provider).__name__)

        # Mark session as processing so UI can detect in-progress state on refresh
        await session.set_processing()

        # Build system prompt
        dynamic_context = await self.build_dynamic_context(session)
        system_prompt = self.context_builder.build_system_prompt(
            dynamic_context=dynamic_context,
        )
        logger.info("System prompt built: %d chars", len(system_prompt))

        # Append user message to conversation (with optional images)
        session.append_user_message(user_message, image_blocks)
        logger.info("Message history: %d messages", len(session.get_messages()))

        # Start the turn counter and persist user message early so it
        # survives LLM failures and connection drops.
        session.start_turn()
        await session.persist_turn_incremental(user_message, "", None)

        turn = 0
        assistant_text_parts: list[str] = []
        # Collects one record per tool call for training/audit storage
        tool_call_log: list[dict[str, Any]] = []
        # Track the model used (from the first LLM response)
        model_used: str | None = None

        while turn < self.max_turns:
            turn += 1
            request_id = f"{session.session_id}_{uuid.uuid4().hex[:8]}"

            # Call LLM with tools
            # When a model override is set, pass it as model_tier — providers
            # treat unknown tier strings as direct model names (see get_model).
            effective_tier = override_model or self.model_tier
            logger.info("Turn %d/%d: calling LLM (model_tier=%s, max_tokens=%d, tools=%d)",
                       turn, self.max_turns, effective_tier, self.max_tokens, len(self._anthropic_tools))
            start_time = time.monotonic()
            response = await provider.create_completion_with_tools(
                system_prompt=system_prompt,
                messages=session.get_messages(),
                tools=self._anthropic_tools,
                model_tier=effective_tier,
                max_tokens=self.max_tokens,
            )
            latency_ms = int((time.monotonic() - start_time) * 1000)
            logger.info("Turn %d: LLM responded in %dms, stop_reason=%s, model=%s",
                       turn, latency_ms, response.get("stop_reason"), response.get("model"))

            content_blocks = response["content"]
            usage = response["usage"]
            usage["latency_ms"] = latency_ms
            stop_reason = response["stop_reason"]
            reasoning_content = response.get("reasoning_content")

            # Track usage and capture model name
            if not model_used:
                model_used = response["model"]
            session.accumulate_usage(usage)
            await session.record_token_usage(usage, request_id, response["model"], provider.name.lower())

            # Stream thinking/reasoning content if present
            if reasoning_content:
                await event_stream.emit_thinking(reasoning_content)

            # Split text blocks (stream) from tool_use blocks (execute)
            tool_use_blocks = await self._process_content_blocks(
                content_blocks, assistant_text_parts, event_stream
            )

            # Save assistant message to conversation history
            session.append_assistant_message(content_blocks, reasoning_content)

            # If no tool calls, we're done
            if stop_reason != "tool_use" or not tool_use_blocks:
                break

            # Execute each tool block, stream result, collect logs
            tool_result_blocks = []
            for tool_block in tool_use_blocks:
                result_block, log_entry = await self._run_tool_block(
                    tool_block, session, event_stream
                )
                tool_result_blocks.append(result_block)
                tool_call_log.append(log_entry)

            # Append tool results to conversation
            session.append_tool_results(tool_result_blocks)

            # Persist incremental progress so data is not lost on disconnect
            partial_summary = "".join(assistant_text_parts) if assistant_text_parts else ""
            await session.persist_turn_incremental(
                user_message, partial_summary, tool_call_log, model_used
            )

        else:
            # Exhausted max_turns
            await event_stream.emit_text(
                f"\n\n[Reached maximum of {self.max_turns} tool-use turns. "
                "Please continue the conversation to proceed.]"
            )

        # Persist the turn summary, tool call log, and context
        assistant_summary = "".join(assistant_text_parts) if assistant_text_parts else ""
        await session.persist_turn(user_message, assistant_summary, tool_call_log or None, model_used)
        await session.save_context()

        # Learning loop: score session and request feedback
        await self._on_loop_complete(session, tool_call_log)
        await event_stream.emit_feedback_request(
            session_id=session.session_id,
            turn_number=session._turn_count,
        )

        # Mark session as completed and emit done
        await session.complete()
        await event_stream.emit_done(
            session_id=session.session_id,
            usage=session.get_usage_summary(),
        )

    async def _process_content_blocks(
        self,
        content_blocks: list[dict[str, Any]],
        assistant_text_parts: list[str],
        event_stream: AgentEventStream,
    ) -> list[dict[str, Any]]:
        """Stream text blocks and collect tool_use blocks from an LLM response."""
        tool_use_blocks = []
        for block in content_blocks:
            if block["type"] == "text":
                text = block["text"]
                if text:
                    # Separate consecutive text messages with a markdown
                    # paragraph break so they don't run together on the client.
                    if assistant_text_parts:
                        text = "\n\n" + text
                    assistant_text_parts.append(text)
                    await event_stream.emit_text(text)
            elif block["type"] == "tool_use":
                tool_use_blocks.append(block)
        return tool_use_blocks

    async def _run_tool_block(
        self,
        tool_block: dict[str, Any],
        session: BaseSession,
        event_stream: AgentEventStream,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Execute one tool_use block, emit SSE events, and return (result_block, log_entry)."""
        tool_name = tool_block["name"]
        tool_input = tool_block["input"]
        tool_use_id = tool_block["id"]

        tool = self.tools.get(tool_name)
        display_name = tool.get_display_name() if tool else tool_name

        await event_stream.emit_tool_start(tool_name, tool_input, tool_use_id, display_name)

        result = await self._execute_tool(tool_name, tool_input, session)
        tool_content = result.to_tool_result_content()

        # Use a short display summary for the SSE event — the UI only
        # shows 80 chars anyway and very large payloads (e.g. full page
        # trees) can fragment SSE lines and stall the spinner.
        display_summary = result.summary or result.error or tool_content
        if len(display_summary) > 200:
            display_summary = display_summary[:200] + "…"

        await event_stream.emit_tool_result(tool_name, result.success, display_summary, tool_use_id)

        # Learning loop: track tool errors for pitfall detection
        if not result.success:
            await self._on_tool_error(tool_name, tool_input, result.error or "Unknown error")

        result_block = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": tool_content,
            "is_error": not result.success,
        }
        log_entry = {
            "tool": tool_name,
            "display_name": display_name,
            "input": tool_input,
            "success": result.success,
            "summary": result.summary or result.error or "",
        }
        return result_block, log_entry

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

    async def build_dynamic_context(self, session: BaseSession) -> str:
        """Build per-request dynamic context string.

        Override in subclasses to add agent-specific context
        (e.g., component catalog, app state, learned knowledge).

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

    async def _on_loop_complete(
        self, session: BaseSession, tool_call_log: list[dict[str, Any]],
    ) -> None:
        """Hook called after the agent loop completes.

        Triggers async outcome scoring (best-effort, non-blocking).
        """
        try:
            from app.learning.outcome import get_outcome_analyzer
            task = asyncio.create_task(
                get_outcome_analyzer().score_session(session.session_id)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        except Exception as e:
            logger.debug("Learning post-hook skipped: %s", e)

    async def _on_tool_error(
        self, tool_name: str, tool_input: dict[str, Any], error: str,
    ) -> None:
        """Hook called when a tool execution fails.

        Records the error pattern for pitfall detection.
        """
        try:
            from app.learning.knowledge import get_knowledge_extractor
            await get_knowledge_extractor().extract_pitfall_from_errors(
                agent_name=self.name,
                tool_name=tool_name,
                error_message=error,
                tool_input=tool_input,
            )
        except Exception as e:
            logger.debug("Tool error tracking skipped: %s", e)

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
