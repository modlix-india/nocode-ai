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
from app.core.compaction import ContextCompactor
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
        router_tool: ToolDefinition | None = None,
        deferred_tools: list[ToolDefinition] | None = None,
        auto_approve: bool = True,
    ) -> None:
        self.name = name
        self.tools = {t.name: t for t in tools}
        self.context_builder = context_builder
        self.model_tier = model_tier
        self.max_turns = max_turns
        self.max_tokens = max_tokens
        self._provider_name = provider

        # Deferred tool loading: core tools are always in the prompt;
        # deferred tools are only included after the LLM discovers them
        # via ToolSearchTool.
        self._core_tools = [t for t in tools if not t.is_deferred]
        self._deferred_tools = deferred_tools or [t for t in tools if t.is_deferred]

        # Register deferred tools in the dispatch map so they can be
        # executed once discovered, even though their schemas aren't in
        # the initial prompt.
        for dt in self._deferred_tools:
            self.tools[dt.name] = dt

        # When True, mutating tools execute without waiting for user confirmation.
        # Set to False for human-supervised mode.
        self._auto_approve = auto_approve

        # Legacy: tool-of-tools router (deprecated, kept for backward compat)
        self._router_tool_name = router_tool.name if router_tool else None
        if router_tool:
            self._anthropic_tools = [router_tool.to_anthropic_tool()]
        else:
            self._anthropic_tools = [t.to_anthropic_tool() for t in self._core_tools]

        # Auto-compaction: summarize old messages when context approaches limit
        from app.config import settings
        self._compactor = ContextCompactor(
            context_limit=settings.CONTEXT_LIMIT_DEFAULT,
            threshold=settings.COMPACTION_THRESHOLD,
            post_compact_budget=settings.POST_COMPACT_BUDGET,
            keep_recent=settings.COMPACTION_KEEP_RECENT,
        )

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
        except asyncio.CancelledError:
            logger.info("Agent '%s' cancelled in session %s", self.name, session.session_id)
            try:
                await session.persist_turn(user_message, "[Stopped by user]", None)
                await session.complete()
                await event_stream.emit_done(
                    session_id=session.session_id,
                    usage=session.total_usage,
                )
            except Exception:
                pass  # Stream may already be closed
            raise
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

        # Store transient loop state for sub-agent delegation
        self._current_provider = provider
        self._current_event_stream = event_stream
        self._current_session = session

        # Mark session as processing so UI can detect in-progress state on refresh
        await session.set_processing()

        # Build system prompt (pass user_message for URL auto-scraping)
        dynamic_context = await self.build_dynamic_context(session, user_message=user_message)
        system_prompt = self.context_builder.build_system_prompt(
            dynamic_context=dynamic_context,
        )
        logger.info("System prompt built: %d chars", len(system_prompt))

        # Inject scraped screenshot as an image block if available
        all_image_blocks = list(image_blocks) if image_blocks else []
        scraped_screenshot = session.context.pop("scraped_screenshot", None)
        if scraped_screenshot:
            all_image_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": scraped_screenshot,
                },
            })
            logger.info("Injected scraped screenshot as image block")

        # Append user message to conversation (with optional images)
        session.append_user_message(user_message, all_image_blocks or None)
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
            # Check for user-initiated cancellation
            if event_stream.is_cancelled:
                await event_stream.emit_text("\n\n[Stopped by user.]")
                break

            turn += 1
            request_id = f"{session.session_id}_{uuid.uuid4().hex[:8]}"

            # Build per-request tool list (core + discovered deferred tools)
            request_tools = self._build_tools_for_request(session)

            # Call LLM with tools
            # When a model override is set, pass it as model_tier — providers
            # treat unknown tier strings as direct model names (see get_model).
            effective_tier = override_model or self.model_tier
            logger.info("Turn %d/%d: calling LLM (model_tier=%s, max_tokens=%d, tools=%d)",
                       turn, self.max_turns, effective_tier, self.max_tokens, len(request_tools))
            start_time = time.monotonic()

            # Wrap LLM call so it can be interrupted by cancellation.
            # asyncio.shield() is NOT used — we WANT CancelledError to propagate.
            llm_coro = provider.create_completion_with_tools(
                system_prompt=system_prompt,
                messages=session.get_messages(),
                tools=request_tools,
                model_tier=effective_tier,
                max_tokens=self.max_tokens,
            )
            llm_task = asyncio.create_task(llm_coro)

            # Poll for cancellation while waiting for the LLM
            while not llm_task.done():
                if event_stream.is_cancelled:
                    llm_task.cancel()
                    break
                await asyncio.sleep(0.5)

            if event_stream.is_cancelled:
                break

            response = await llm_task
            latency_ms = int((time.monotonic() - start_time) * 1000)

            # Check cancellation right after LLM returns
            if event_stream.is_cancelled:
                break

            content_blocks = response["content"]
            usage = response["usage"]
            usage["latency_ms"] = latency_ms
            stop_reason = response["stop_reason"]
            reasoning_content = response.get("reasoning_content")

            # Summarize what the LLM produced this turn
            text_len = sum(len(b.get("text", "")) for b in content_blocks if b.get("type") == "text")
            tool_names = [b.get("name", "?") for b in content_blocks if b.get("type") == "tool_use"]
            logger.info("Turn %d: %dms, stop=%s | text=%d chars, tools=%s",
                       turn, latency_ms, stop_reason,
                       text_len, ", ".join(tool_names) if tool_names else "(none)")

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
                if event_stream.is_cancelled:
                    break
                result_block, log_entry = await self._run_tool_block(
                    tool_block, session, event_stream
                )
                tool_result_blocks.append(result_block)
                tool_call_log.append(log_entry)

            if event_stream.is_cancelled:
                await event_stream.emit_text("\n\n[Stopped by user.]")
                break

            # Log a concise turn summary
            for entry in tool_call_log[-len(tool_result_blocks):]:
                status = "OK" if entry.get("success") else "FAIL"
                summary = entry.get("summary", "")
                if len(summary) > 120:
                    summary = summary[:120] + "..."
                logger.info("  [%s] %s → %s: %s", status, entry.get("tool", "?"),
                            entry.get("display_name", ""), summary)

            # Append tool results to conversation
            session.append_tool_results(tool_result_blocks)

            # Auto-compact if context is approaching the limit
            await self._check_and_compact(session, provider)

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

    # Tools that require user confirmation before execution.
    # Includes both core CRUD tools and deferred tools that mutate data.
    CONFIRMATION_TOOLS: set[str] = {
        # Core CRUD
        "create", "update", "delete", "copy",
        # Deferred page tools
        "patch_components", "patch_event",
        # Deferred theme/style tools (themes require extra care)
        "create_theme", "update_theme", "create_style", "update_style",
        # Deferred function/schema tools
        "create_function", "update_function",
        "create_schema", "update_schema",
        # Deferred data tools
        "create_connection", "update_connection",
        "create_uripath", "update_uripath",
        "manage_template",
        # Deferred app config tools
        "update_app_pages", "update_app_fonts", "update_app_meta",
        # Deferred orchestration
        "delegate_task",
    }

    def _build_confirmation_message(
        self, tool_name: str, display_name: str, tool_input: dict[str, Any],
    ) -> str:
        """Build a human-readable confirmation message from tool input."""
        object_type = tool_input.get("object_type", "object")
        name = tool_input.get("name") or tool_input.get("page_name") or tool_input.get("id") or "?"
        message = tool_input.get("message", "")

        if tool_name == "create":
            return f"Create {object_type} '{name}'" + (f" — {message}" if message else "")
        if tool_name == "update":
            parts = []
            if tool_input.get("properties"):
                parts.append(f"properties: {list(tool_input['properties'].keys())}")
            if tool_input.get("operations"):
                ops = tool_input["operations"]
                op_summary = ", ".join(
                    f"{op.get('op', '?')} '{op.get('component_key', op.get('parent_key', '?'))}'"
                    for op in ops[:5]
                )
                if len(ops) > 5:
                    op_summary += f", +{len(ops) - 5} more"
                parts.append(f"component ops: [{op_summary}]")
            if tool_input.get("event_function"):
                fn_name = tool_input["event_function"].get("function_name", "?")
                parts.append(f"event function: {fn_name}")
            if tool_input.get("delete_event_function"):
                parts.append(f"delete event: {tool_input['delete_event_function']}")
            if tool_input.get("definition"):
                parts.append("definition update")
            detail = "; ".join(parts) if parts else message
            return f"Update {object_type} '{name}'" + (f" — {detail}" if detail else "")
        if tool_name == "delete":
            return f"Delete {object_type} '{name}'"
        if tool_name == "copy":
            src_name = tool_input.get("source_name", "?")
            src_app = tool_input.get("source_app_code", "?")
            tgt_app = tool_input.get("target_app_code", "?")
            tgt_name = tool_input.get("target_name") or src_name
            if tool_input.get("source_component_key"):
                return (
                    f"Copy subtree '{tool_input['source_component_key']}' from "
                    f"{object_type} '{src_name}' in app '{src_app}' into page "
                    f"'{tool_input.get('target_page_name', '?')}' in app '{tgt_app}'"
                )
            if object_type == "application":
                return f"Copy application '{src_app}' to new app '{tgt_app}'"
            return f"Copy {object_type} '{src_name}' from app '{src_app}' to app '{tgt_app}' as '{tgt_name}'"
        return f"{display_name} on {object_type} '{name}'"

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

        # Legacy: unwrap tool-of-tools router if still active
        if self._router_tool_name and tool_name == self._router_tool_name:
            tool_name = tool_input.get("tool", tool_name)
            tool_input = tool_input.get("params", {})

        tool = self.tools.get(tool_name)
        display_name = tool.get_display_name() if tool else tool_name

        await event_stream.emit_tool_start(tool_name, tool_input, tool_use_id, display_name)

        # Request user confirmation for mutating operations.
        # In auto_approve mode (default for AI-driven sessions), skip the
        # confirmation SSE round-trip and execute immediately.
        if tool_name in self.CONFIRMATION_TOOLS and not self._auto_approve:
            confirmation_id = f"confirm_{tool_use_id}"
            confirm_msg = self._build_confirmation_message(tool_name, display_name, tool_input)
            confirmation = await event_stream.request_confirmation(
                confirmation_id=confirmation_id,
                message=confirm_msg,
                tool_name=tool_name,
                display_name=display_name,
                details=tool_input,
                session_id=session.session_id,
            )
            if not confirmation.get("approved"):
                reason = confirmation.get("reason", "User denied the operation")
                result = ToolResult(success=False, error=f"Operation denied: {reason}")
                await event_stream.emit_tool_result(tool_name, False, f"Denied: {reason}", tool_use_id)
                result_block = {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": result.to_tool_result_content(),
                    "is_error": True,
                }
                log_entry = {
                    "tool": tool_name,
                    "display_name": display_name,
                    "input": tool_input,
                    "success": False,
                    "summary": f"Denied: {reason}",
                }
                return result_block, log_entry

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

    async def build_dynamic_context(self, session: BaseSession, user_message: str = "") -> str:
        """Build per-request dynamic context string.

        Override in subclasses to add agent-specific context
        (e.g., component catalog, app state, learned knowledge).

        Args:
            session: Active session with auth context.
            user_message: The current user message (not yet in session.messages).

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

    async def _check_and_compact(self, session: BaseSession, provider: Any) -> None:
        """Check if compaction is needed and run it if so.

        Uses the same LLM provider but with the fast model tier for
        cost-effective summarization.
        """
        if not self._compactor.should_compact(session):
            return

        # Get the definition cache from the tool context if available
        definition_cache = None
        ctx = self.build_tool_context(session)
        if "definition_cache" in ctx:
            definition_cache = ctx["definition_cache"]

        # Reuse the same provider — the compactor calls create_completion
        # with the fast model tier, not create_completion_with_tools
        await self._compactor.compact(session, provider, definition_cache)

    def _build_tools_for_request(self, session: BaseSession) -> list[dict[str, Any]]:
        """Build the tool schemas to send in this LLM call.

        Includes all core tools plus any deferred tools that the LLM has
        already discovered via ToolSearchTool.  This keeps the initial
        prompt lean while making discovered tools available in subsequent
        turns.
        """
        if self._router_tool_name:
            # Legacy router mode: just return the single router schema
            return self._anthropic_tools

        discovered: list[str] = session.context.get("discovered_tools", [])
        if not discovered:
            return self._anthropic_tools

        # Start with core tools
        schemas = list(self._anthropic_tools)

        # Add discovered deferred tools
        for dt in self._deferred_tools:
            if dt.name in discovered:
                schemas.append(dt.to_anthropic_tool())

        return schemas

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
            "deferred_tools": self._deferred_tools,
        }
        if session.auth:
            ctx["headers"] = session.auth.to_headers()
            ctx["client_code"] = session.auth.client_code
            ctx["app_code"] = session.auth.app_code
            if session.auth.path_prefix:
                ctx["path_prefix"] = session.auth.path_prefix
            if session.auth.referer:
                ctx["referer"] = session.auth.referer
        return ctx
