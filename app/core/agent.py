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
import json
import logging
import re
import time
import uuid
from typing import Any

from app.core.tools.base import ToolDefinition, ToolResult
from app.core.streaming import AgentEventStream, current_agent_id
from app.core.session import BaseSession, current_session
from app.core.context import BaseContext
from app.core.builtin_tools import (
    close_builtin_rows, on_builtin_tool_result, on_builtin_tool_use,
)
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

    # Friendly label shown in the agent card header. Subclasses override.
    # Defaults to a Title-Cased version of `name` if not set.
    display_name: str | None = None

    def __init__(
        self,
        name: str,
        tools: list[ToolDefinition],
        context_builder: BaseContext,
        model_tier: str = "balanced",
        max_turns: int = 50,
        max_tokens: int = 16384,
        provider: str | None = None,
        context_management: dict | None = None,
        router_tool: ToolDefinition | None = None,
    ) -> None:
        self.name = name
        self.tools = {t.name: t for t in tools}
        self.context_builder = context_builder
        self.model_tier = model_tier
        self.max_turns = max_turns
        self.max_tokens = max_tokens
        self._provider_name = provider
        self.context_management = context_management
        if not self.display_name:
            self.display_name = name.replace("_", " ").title()

        # Tool-of-tools: LLM sees only the router schema; unwrap
        # execute(tool=X, params={}) → real call at dispatch (_run_tool_block).
        self._router_tool_name = router_tool.name if router_tool else None
        if router_tool:
            self._anthropic_tools = [router_tool.to_anthropic_tool()]
        else:
            self._anthropic_tools = [t.to_anthropic_tool() for t in tools]

        # Registry lint: a CONFIRMATION_TOOLS entry pauses for the user, so it
        # must declare kind='elicitation', elicit_mode='blocking' (keeps the
        # one-ask-per-turn serialization honest). Warn on any that forgot.
        for _cname in self.CONFIRMATION_TOOLS:
            _ct = self.tools.get(_cname)
            if _ct is not None and getattr(_ct, "kind", "tool") != "elicitation":
                logger.warning(
                    "unmarked_elicitation_tool: agent=%s tool=%s is in "
                    "CONFIRMATION_TOOLS but kind!='elicitation' — mark it "
                    "kind='elicitation', elicit_mode='blocking'",
                    name, _cname,
                )

        # strong refs so tasks aren't GC'd mid-flight
        self._background_tasks: set[asyncio.Task] = set()

    async def run(
        self,
        user_message: str,
        session: BaseSession,
        event_stream: AgentEventStream,
        image_blocks: list[dict[str, Any]] | None = None,
        model_override: str | None = None,
    ) -> None:
        """Execute the agentic loop for one user request (spans many internal turn
        iterations). Builds the prompt, appends the user message, runs the tool-use
        loop, emits done. All errors are caught and emitted as error events.

        Args:
            user_message: The user's input text.
            session: Active session with auth context and message history.
            event_stream: SSE stream to emit events to the client.
            image_blocks: Optional image content blocks (Anthropic format).
            model_override: Optional "provider:model" override of the default.
        """
        # Nested sub-agent run? The AgentCard lifecycle (agent_started/finished)
        # is owned by the spawning tool, not here — the launcher pre-emits started
        # and emits finished after post-processing, so the card outlives this loop.
        # is_sub_agent is used ONLY for error routing below: a sub-agent re-raises
        # to its parent's tool wrapper; the top-level "root" agent emits error/done
        # itself to close the SSE stream.
        is_sub_agent = current_agent_id.get() != "root"
        ctx_token = current_agent_id.set(self.name)
        # Bill standalone one-shot LLM calls (outside the loop) to this agent's session.
        sess_token = current_session.set(session)

        try:
            try:
                logger.info("Agent '%s' run: session=%s, provider=%s, model_override=%s, images=%s",
                           self.name, session.session_id, self._provider_name or "(default)",
                           model_override or "(none)",
                           len(image_blocks) if image_blocks else 0)
                await self._run_loop(user_message, session, event_stream, image_blocks, model_override)
            except asyncio.CancelledError:
                logger.info("Agent '%s' cancelled in session %s", self.name, session.session_id)
                if is_sub_agent:
                    raise
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
                if is_sub_agent:
                    # Let the parent's tool wrapper catch & convert to ToolResult.
                    raise
                # Top-level: emit error + done so the SSE stream closes cleanly.
                logger.exception("Agent '%s' error in session %s", self.name, session.session_id)
                error_text = f"Agent error: {type(e).__name__}: {e}"
                await event_stream.emit_error(error_text)
                await session.persist_turn(user_message, error_text, None)
                await session.complete()
                await event_stream.emit_done(
                    session_id=session.session_id,
                    usage=session.total_usage,
                )
        finally:
            # agent_finished is NOT emitted here. The spawning tool is
            # responsible for emitting it after all post-processing completes.
            # This ensures the agent row stays "running" through the full
            # user-visible lifecycle (craft emission, summary streaming, etc.),
            # not just the LLM tool-use loop.
            current_agent_id.reset(ctx_token)
            current_session.reset(sess_token)

    async def _run_loop(
        self,
        user_message: str,
        session: BaseSession,
        event_stream: AgentEventStream,
        image_blocks: list[dict[str, Any]] | None = None,
        model_override: str | None = None,
    ) -> None:
        """Internal agentic loop implementation."""
        session.context.pop("_pending_suggestions", None)

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

        # Append user message + start turn BEFORE build_dynamic_context, so it
        # reads THIS turn's message (session.messages) and turn count — else the
        # context shows the previous message with an off-by-one turn.
        session.append_user_message(user_message, image_blocks)
        logger.info("Message history: %d messages", len(session.get_messages()))
        session.start_turn()
        await session.persist_turn_incremental(user_message, "", None)

        # Layer 1 — system-prompt context: build_dynamic_context runs ONCE per
        # request and is folded into the (cacheable) system prompt. Agents whose
        # context is fully per-turn (e.g. adzump) return "" here and put their
        # steering in the per-turn reminder (Layer 2, in the loop) instead.
        dynamic_context = await self.build_dynamic_context(session)
        system_prompt = self.context_builder.build_system_prompt(
            dynamic_context=dynamic_context,
        )
        logger.info("System prompt built: %d block(s)", len(system_prompt))

        turn = 0
        assistant_text_parts: list[str] = []
        # Collects one record per tool call for training/audit storage
        tool_call_log: list[dict[str, Any]] = []
        # Track the model used (from the first LLM response)
        model_used: str | None = None
        # Deterministic stuck-loop breaker (run-scoped). When a turn's tool calls
        # ALL fail with the same tool-name signature N turns running, the offending
        # tool(s) are quarantined (withdrawn from the tool set) for the rest of THIS
        # run — so the model is forced onto a different action (e.g. asking the
        # user) instead of re-calling a tool that keeps rejecting (an advisory
        # STOP-steer the model ignores). Signature = tool NAMES, not inputs (a model
        # inventing fresh values each turn keeps the same names); any success resets
        # it. N=4 sits one above the domain advisory's n>=3.
        STUCK_N = 4
        stuck_sig: tuple[str, ...] | None = None
        stuck_n = 0
        quarantined: set[str] = set()

        while turn < self.max_turns:
            if event_stream.is_cancelled:
                await event_stream.emit_text("\n\n[Stopped by user.]")
                break

            turn += 1
            request_id = f"{session.session_id}_{uuid.uuid4().hex[:8]}"

            effective_tier = override_model or self.model_tier
            logger.info("Turn %d/%d: calling LLM (model_tier=%s, max_tokens=%d, tools=%d)",
                       turn, self.max_turns, effective_tier, self.max_tokens, len(self._anthropic_tools))
            start_time = time.monotonic()

            # Pre-call phase — decorate the per-call messages before the request
            # (Layer 2 per-turn steering injected at the tail). See _apply_pre_call.
            call_messages = await self._apply_pre_call(session, turn)

            # Stream the turn + assemble the provider chunks into blocks. Mutates
            # assistant_text_parts (run-scoped) in place; always drains builtin
            # rows, even on a mid-stream raise. See _stream_turn.
            # Withdraw quarantined tools — filtered COPY, never mutate self._anthropic_tools.
            call_tools = (
                [t for t in self._anthropic_tools if t.get("name") not in quarantined]
                if quarantined else None
            )
            content_blocks, tool_use_blocks, stop_reason, usage, _text_chunk_count = (
                await self._stream_turn(
                    provider, system_prompt, call_messages, effective_tier,
                    event_stream, assistant_text_parts, tools=call_tools,
                )
            )

            latency_ms = int((time.monotonic() - start_time) * 1000)
            usage["latency_ms"] = latency_ms
            logger.info("Turn %d: LLM streamed in %dms, stop_reason=%s, text_chunks=%d, usage=%s",
                       turn, latency_ms, stop_reason, _text_chunk_count, usage)
            if stop_reason == "max_tokens":
                logger.warning(
                    "Turn %d truncated at max_tokens=%d — response incomplete. "
                    "Increase max_tokens or tighten the prompt's output.",
                    turn, self.max_tokens,
                )

            resolved_model = provider.get_model(effective_tier)
            if not model_used:
                model_used = resolved_model
            session.accumulate_usage(usage)
            await session.record_token_usage(usage, request_id, resolved_model, provider.name.lower())

            # Reasoning models (e.g. MiniMax M3) return interleaved reasoning in
            # usage["reasoning_content"]; the API requires it echoed back on the next
            # turn, so persist it on the assistant message and let the provider re-emit it.
            reasoning_content = usage.pop("reasoning_content", None) if isinstance(usage, dict) else None

            session.append_assistant_message(content_blocks, reasoning_content)

            if stop_reason != "tool_use" or not tool_use_blocks:
                break

            if event_stream.is_cancelled:
                break

            logger.info("Turn %d: executing %d tool(s) %s: %s",
                        turn, len(tool_use_blocks),
                        "in parallel" if len(tool_use_blocks) > 1 else "",
                        [tb.get("name", "?") for tb in tool_use_blocks])

            # THIS turn's streamed prose (before tool calls) so a widget tool can skip
            # re-emitting text the model already wrote (e.g. present_options de-dupes a
            # question streamed as its lead-in). content_blocks resets per turn, so this
            # is current-turn only (assistant_text_parts is whole-run). Transient attr,
            # not persisted; generic + inert — only a tool that opts to read it cares.
            session._turn_assistant_text = "".join(
                b.get("text", "") for b in content_blocks if b.get("type") == "text"
            )

            # Telemetry + kill-switch for the parallel-batch elicitation race. The LLM
            # DOES batch >1 tool_use when several items are missing (observed live:
            # two widgets stacked in one bubble). force_serial_on_elicitation early-exits
            # after the first deferred elicitation so a second can't stack; this warning
            # fires regardless, for visibility into batch frequency.
            batch_has_elicitation = self._batch_has_deferred_elicitation(tool_use_blocks)
            if len(tool_use_blocks) > 1 and batch_has_elicitation:
                logger.warning(
                    "stacked_elicitation_batch: turn=%d tools=%s force_serial=%s · "
                    "LLM emitted multiple tool_use blocks incl. a deferred "
                    "elicitation; widgets may stack unless force_serial_on_elicitation is on",
                    turn, [tb.get("name", "?") for tb in tool_use_blocks],
                    self.force_serial_on_elicitation,
                )

            run_serial = (
                len(tool_use_blocks) == 1
                or (self.force_serial_on_elicitation and batch_has_elicitation)
            )
            if run_serial:
                tool_result_blocks = []
                # Serial dispatch EARLY-EXITS after the first deferred elicitation:
                # remaining batched tools aren't run (no second stacked widget), but each
                # gets a placeholder tool_result (Anthropic requires one per tool_use
                # block). The LLM re-issues deferred calls next turn if still needed.
                # Reached via force_serial_on_elicitation (opt-in) or the single-tool case.
                stop_batch = False
                for tb in tool_use_blocks:
                    if stop_batch:
                        tool_result_blocks.append({
                            "type": "tool_result",
                            "tool_use_id": tb.get("id", ""),
                            "content": (
                                "Deferred: an earlier tool in this turn asked the "
                                "user a question, so this call was not run. Re-issue "
                                "it after the user replies if it's still needed."
                            ),
                            "is_error": False,
                        })
                        tool_call_log.append({
                            "tool": tb.get("name", ""),
                            "display_name": tb.get("name", ""),
                            "input": tb.get("input", {}),
                            "success": False,
                            "summary": "deferred (batched after an elicitation)",
                            "tool_use_id": tb.get("id", ""),
                            "kind": "tool", "elicit_mode": "deferred",
                            "elicited": False, "elicit_expects": "single",
                        })
                        continue
                    result_block, log_entry = await self._run_tool_block(
                        tb, session, event_stream, assistant_text_parts
                    )
                    tool_result_blocks.append(result_block)
                    tool_call_log.append(log_entry)
                    if self._is_deferred_elicitation(log_entry):
                        stop_batch = True
            else:
                results = await asyncio.gather(
                    *(self._run_tool_block(tb, session, event_stream, assistant_text_parts)
                      for tb in tool_use_blocks),
                    return_exceptions=False,
                )
                tool_result_blocks = [r[0] for r in results]
                for _, log_entry in results:
                    tool_call_log.append(log_entry)

            if event_stream.is_cancelled:
                await event_stream.emit_text("\n\n[Stopped by user.]")
                break

            session.append_tool_results(tool_result_blocks)

            # Persist incremental progress so data is not lost on disconnect
            partial_summary = "".join(assistant_text_parts) if assistant_text_parts else ""
            await session.persist_turn_incremental(
                user_message, partial_summary, tool_call_log, model_used
            )

            # Elicitation turn boundary + lifecycle.
            new_entries = tool_call_log[-len(tool_result_blocks):]
            # Multi-reply elicitation (e.g. asset uploads): if one is open and
            # this turn ran a tool other than its opener, the LLM has moved on
            # to real work → close it. (A new deferred elicitation below would
            # overwrite it anyway.)
            pending = session.context.get("_pending_elicitation")
            if pending and pending.get("expects") == "multi":
                ran = {e.get("tool") for e in new_entries}
                if pending.get("tool") not in ran:
                    session.context.pop("_pending_elicitation", None)
            # Break after a DEFERRED elicitation so the turn yields to the user
            # (one ask per turn). Blocking elicitations are excluded — they
            # already paused in-tool and resolved before returning. save_context()
            # after the for-else (below) persists _pending_elicitation across
            # disconnects/refreshes (message history drops tool blocks; context
            # survives — see session.py).
            elicited = next(
                (e for e in new_entries if self._is_deferred_elicitation(e)), None
            )
            if elicited:
                session.context["_pending_elicitation"] = {
                    "id": f"elicit_{elicited.get('tool_use_id', '')}",
                    "tool": elicited.get("tool", ""),
                    "expects": elicited.get("elicit_expects", "single"),
                    "opened_turn": turn,
                    # Carry the optional answer tag (None for untagged
                    # elicitations); a subclass may consume it next turn to
                    # capture the reply deterministically. Core never reads it.
                    "field": elicited.get("elicit_field"),
                    "answers": elicited.get("elicit_answers"),
                    # Generic typed payload (see elicit_payload above); the
                    # subclass that opened the elicitation reads + mutates it
                    # across turns. Core only carries + persists it.
                    "payload": elicited.get("elicit_payload"),
                }
                logger.info(
                    "elicitation_break: turn=%d tool=%s expects=%s batch=%d",
                    turn, elicited.get("tool"),
                    elicited.get("elicit_expects"), len(tool_result_blocks),
                )
                break

            # Stuck-loop detection (see _stuck_step). On trip, quarantine
            # offenders for the rest of the run so the model must ask/move on.
            stuck_sig, stuck_n, to_quarantine = self._stuck_step(
                new_entries, stuck_sig, stuck_n, STUCK_N
            )
            newly = to_quarantine - quarantined
            if newly:
                quarantined |= newly
                logger.warning(
                    "stuck_loop_quarantine: turn=%d tools=%s — withdrawing for the "
                    "rest of this run so the model must ask/move on", turn, sorted(newly),
                )

        else:
            # while/else: reached ONLY when the loop ends without a break —
            # i.e. true max-turns exhaustion. Every break above (cancel, no
            # tool calls, deferred elicitation) skips this notice.
            await event_stream.emit_text(
                f"\n\n[Reached maximum of {self.max_turns} tool-use turns. "
                "Please continue the conversation to proceed.]"
            )

        # Persist the turn summary, tool call log, and context
        assistant_summary = "".join(assistant_text_parts) if assistant_text_parts else ""
        await session.persist_turn(user_message, assistant_summary, tool_call_log or None, model_used)
        await session.save_context()

        suggestions = await self.get_pending_suggestions(session, assistant_summary)
        if suggestions:
            await event_stream.emit_suggestions(**suggestions)

        # Learning loop: score session and request feedback
        await self._on_loop_complete(session, tool_call_log)
        await event_stream.emit_feedback_request(
            session_id=session.session_id,
            turn_number=session._turn_count,
        )

        await session.complete()
        await event_stream.emit_done(
            session_id=session.session_id,
            usage=session.get_usage_summary(),
        )

    async def _stream_turn(
        self,
        provider,
        system_prompt: list[dict[str, Any]],
        call_messages: list[dict[str, Any]],
        effective_tier: str,
        event_stream: AgentEventStream,
        assistant_text_parts: list[str],
        tools: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, dict[str, Any], int]:
        """Stream one LLM turn; assemble provider chunks into blocks.

        Returns ``(content_blocks, tool_use_blocks, stop_reason, usage, text_chunks)``.
        Mutates ``assistant_text_parts`` in place — run-scoped (read at finalize), NOT
        a per-turn output, so it must not be returned fresh. Builtin rows ALWAYS drain
        via the finally, even on a mid-stream raise, so no row spins forever in the UI.
        Returns normally on cancel so the loop's post-stream cancel checks still fire.
        """
        content_blocks: list[dict[str, Any]] = []
        tool_use_blocks: list[dict[str, Any]] = []
        current_text = ""
        current_tool: dict[str, Any] | None = None
        stop_reason = "end_turn"
        usage: dict[str, Any] = {}
        text_chunks = 0
        # Per-tool-id state for builtin (server-executed) rows. Anthropic
        # streams all tool_use blocks first, then all result blocks — so we
        # can't use a single "active" slot like OpenAI's interleaved pattern.
        # Each row stays open from builtin_tool_use until the finally drains it.
        # Shape: ``{tool_id: {"name": str, "summary": str}}``.
        builtin_rows: dict[str, dict[str, Any]] = {}

        try:
            async for chunk in provider.stream_completion_with_tools(
                system_prompt=system_prompt,
                messages=call_messages,
                tools=self._anthropic_tools if tools is None else tools,
                model_tier=effective_tier,
                max_tokens=self.max_tokens,
                context_management=self.context_management,
            ):
                # Honor user "stop" — break out of the streaming loop.
                if event_stream.is_cancelled:
                    break
                if chunk.type == "text_delta":
                    text_chunks += 1
                    current_text += chunk.text
                    await event_stream.emit_text(chunk.text)

                elif chunk.type == "reasoning_delta":
                    await event_stream.emit_thinking(chunk.text)

                elif chunk.type == "builtin_tool_use":
                    # Server-executed builtin (web_search / web_fetch). Row
                    # tracking, rendering and SSE emits live in core/builtin_tools.
                    await on_builtin_tool_use(builtin_rows, chunk, event_stream)

                elif chunk.type == "builtin_tool_result":
                    # Render hits/content onto the matching row (by tool_id) and
                    # emit the delta. Orphaned-result crash guard + the
                    # delta-not-cumulative rule live in core/builtin_tools.
                    await on_builtin_tool_result(builtin_rows, chunk, event_stream)

                elif chunk.type == "tool_use_start":
                    # Flush text block (scrub leaked tool-call syntax).
                    if current_text:
                        cleaned = self._clean_assistant_text(current_text)
                        if cleaned:
                            content_blocks.append({"type": "text", "text": cleaned})
                            assistant_text_parts.append(cleaned)
                        current_text = ""
                    current_tool = {
                        "type": "tool_use",
                        "id": chunk.tool_id,
                        "name": chunk.tool_name,
                        "input": {},
                    }

                elif chunk.type == "tool_input_delta":
                    if current_tool:
                        current_tool["_input_json"] = current_tool.get("_input_json", "") + chunk.tool_input_json

                elif chunk.type == "tool_use_end":
                    if current_tool:
                        raw = current_tool.pop("_input_json", "{}")
                        try:
                            current_tool["input"] = json.loads(raw)
                        except (ValueError, json.JSONDecodeError):
                            current_tool["input"] = {}
                        content_blocks.append(current_tool)
                        tool_use_blocks.append(current_tool)
                        current_tool = None

                elif chunk.type == "message_complete":
                    # Authoritative assembled content from the provider
                    # (e.g. Anthropic stream.get_final_message()). Replaces
                    # event-driven reconstruction so opaque server-tool
                    # blocks (server_tool_use / web_search_tool_result)
                    # arrive intact in their original position.
                    if chunk.blocks:
                        content_blocks, tool_use_blocks = self._adopt_final_blocks(
                            chunk.blocks, assistant_text_parts,
                        )
                        current_text = ""
                        current_tool = None

                elif chunk.type == "done":
                    stop_reason = chunk.stop_reason or "end_turn"
                    usage = chunk.usage or {}
                    break
        finally:
            # Close every builtin row opened this turn (flush final summaries).
            # In a finally so a mid-stream raise can't leave a row spinning.
            await close_builtin_rows(builtin_rows, event_stream)

        # Flush remaining text (scrub leaked tool-call syntax).
        if current_text:
            cleaned = self._clean_assistant_text(current_text)
            if cleaned:
                content_blocks.append({"type": "text", "text": cleaned})
                assistant_text_parts.append(cleaned)

        return content_blocks, tool_use_blocks, stop_reason, usage, text_chunks

    def _adopt_final_blocks(
        self,
        blocks: list[dict[str, Any]],
        assistant_text_parts: list[str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Use an authoritative final-message block list in place of event reconstruction.

        Returns ``(content_blocks, tool_use_blocks)`` and mutates
        ``assistant_text_parts`` in place so persistence/summaries stay in sync.
        Scrub leaked tool-call syntax from text blocks here too — this is the
        Anthropic message_complete path and rebuilds the parts authoritatively, so
        it must scrub or the event-path scrub is bypassed.
        """
        content_blocks = [dict(b) for b in blocks]
        for b in content_blocks:
            if b.get("type") == "text" and b.get("text"):
                b["text"] = self._clean_assistant_text(b["text"])
        tool_use_blocks = [b for b in content_blocks if b.get("type") == "tool_use"]
        assistant_text_parts[:] = [
            b["text"] for b in content_blocks
            if b.get("type") == "text" and b.get("text")
        ]
        return content_blocks, tool_use_blocks

    # Tool names that pause for user confirmation before execution. The
    # confirmation *mechanism* (the registry lint above + the request_confirmation
    # flow in _run_tool_block) is generic and lives here; *which* tools are
    # mutating is domain-specific, so the default is empty. Subclasses that own
    # external-state mutations declare their own set (e.g. AppBuilderAgent's CRUD).
    CONFIRMATION_TOOLS: set[str] = set()

    # Kill-switch for the parallel-elicitation race. When True, a tool batch
    # containing a deferred elicitation runs serially instead of via
    # asyncio.gather, so a stacked widget can't stream before the break check.
    # Default False; flip per-subclass (or globally) if the
    # stacked_elicitation_batch telemetry ever fires.
    force_serial_on_elicitation: bool = False

    @staticmethod
    def _is_deferred_elicitation(log_entry: dict[str, Any]) -> bool:
        """True if a completed tool was a deferred elicitation — either by
        static declaration (kind='elicitation', elicit_mode='deferred') or by
        a runtime signal (ToolResult.data['elicited']=True, e.g. analyze_product
        when assets are missing). Blocking elicitations are excluded."""
        static = (
            log_entry.get("kind") == "elicitation"
            and log_entry.get("elicit_mode") == "deferred"
        )
        return static or log_entry.get("elicited") is True

    def _batch_has_deferred_elicitation(
        self, tool_use_blocks: list[dict[str, Any]],
    ) -> bool:
        """True if any block names a statically-declared deferred elicitation
        tool. Runtime-conditional elicitations (analyze_product) are unknown
        pre-execution, so this static check serves only the parallel
        kill-switch + telemetry."""
        for tb in tool_use_blocks:
            name = tb.get("name", "")
            if self._router_tool_name and name == self._router_tool_name:
                name = (tb.get("input") or {}).get("tool", name)
            tool = self.tools.get(name)
            if (
                tool is not None
                and getattr(tool, "kind", "tool") == "elicitation"
                and getattr(tool, "elicit_mode", "deferred") == "deferred"
            ):
                return True
        return False

    def _build_confirmation_message(
        self, tool_name: str, display_name: str, tool_input: dict[str, Any],
    ) -> str:
        """Human-readable confirmation prompt for a CONFIRMATION_TOOLS call.

        Override in subclasses to describe the specific mutation in domain terms;
        the default is a generic prompt. Called from _run_tool_block only when
        tool_name is in CONFIRMATION_TOOLS (empty by default), so the base
        implementation is reached only by a subclass that sets the tool set but
        not this hook.
        """
        return f"Confirm: {display_name}"

    @staticmethod
    def _stuck_step(
        new_entries: list[dict[str, Any]],
        prev_sig: tuple[str, ...] | None,
        prev_n: int,
        n_threshold: int,
    ) -> tuple[tuple[str, ...] | None, int, set[str]]:
        """One stuck-loop step (pure → unit-tested below the model).

        A turn is "stuck" when EVERY tool call in it made NO PROGRESS — either it
        failed, OR it succeeded but stored nothing new (a kept-noop, flagged
        ``no_progress``). Both read as "retry-me" to a model that ignores the
        steer. Signature is the sorted tool-name tuple (NOT inputs — a model
        inventing fresh values keeps the same names). Returns
        ``(sig, n, to_quarantine)``: the running signature + consecutive count,
        and — once the SAME stuck signature repeats ``n_threshold`` turns — the
        set of no-progress tool names to withdraw (resets so a different tool can
        re-trip). RESETS on real progress: any success that stored new work, OR
        any elicitation (asking the user advanced the conversation), OR an empty
        turn — preserving the normal self-heal path."""
        # Asking the user IS progress — a turn that elicits is never stuck.
        if any(e.get("elicited") for e in new_entries):
            return None, 0, set()
        stuck = [e for e in new_entries if (not e.get("success")) or e.get("no_progress")]
        sig = (
            tuple(sorted(e.get("tool", "") for e in new_entries))
            if new_entries and len(stuck) == len(new_entries) else None
        )
        n = (prev_n + 1) if (sig and sig == prev_sig) else (1 if sig else 0)
        if sig and n >= n_threshold:
            return None, 0, {e.get("tool", "") for e in stuck if e.get("tool")}
        return sig, n, set()

    @staticmethod
    def _strip_tool_syntax(text: str, tool_names: set[str]) -> tuple[str, int]:
        """Remove standalone leaked tool-call-syntax lines from user-facing
        assistant text — e.g. a model echoing the internal prescription
        `present_options(question=…, field="duration")` into chat. Returns
        ``(cleaned, n_stripped)``. Pure → unit-tested below the model.

        Tight to avoid eating legit prose: a line is stripped ONLY if it is, on
        its own line, ``<known_tool>(`` followed by a kwarg (``foo=``) or an empty
        call ``()``. So registered tool names only (not generic ``foo(``), and
        "I'll use present_options for you" (no paren/kwarg) survives untouched."""
        if not text or not tool_names:
            return text, 0
        names = "|".join(re.escape(n) for n in sorted(tool_names))
        # ^ line start (+ optional backticks/bullet) · known tool name · "(" ·
        # then a kwarg (word=) OR close-paren · rest of the (possibly wrapped) line.
        pat = re.compile(
            rf"(?im)^[ \t]*`{{0,3}}[ \t]*(?:{names})\([ \t]*(?:[a-z_]+[ \t]*=|\))[^\n]*$\n?"
        )
        cleaned, n = pat.subn("", text)
        if n:
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned, n

    def _clean_assistant_text(self, text: str) -> str:
        """Scrub leaked tool-call syntax from one assistant text block, keyed off
        the live tool registry. Logs when it fires (telemetry on prompt-guard
        decay) so the leak is a counted alarm, not a silent spot-check."""
        cleaned, n = self._strip_tool_syntax(text, set(self.tools))
        if n:
            logger.warning("stripped_tool_syntax_leak: removed %d line(s) of "
                           "tool-call syntax from assistant text", n)
        return cleaned

    async def _run_tool_block(
        self,
        tool_block: dict[str, Any],
        session: BaseSession,
        event_stream: AgentEventStream,
        assistant_text_parts: list[str],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Execute one tool_use block, emit SSE events, and return (result_block, log_entry).

        ``assistant_text_parts`` is the run-scoped list the persisted turn is
        built from; a tool whose ``audience`` targets the user appends its summary
        here so the receipt survives refresh (see the audience block below)."""
        tool_name = tool_block["name"]
        tool_input = tool_block["input"]
        tool_use_id = tool_block["id"]

        # Unwrap tool-of-tools router: execute(tool="read", params={...}) → read({...})
        if self._router_tool_name and tool_name == self._router_tool_name:
            tool_name = tool_input.get("tool", tool_name)
            tool_input = tool_input.get("params", {})

        tool = self.tools.get(tool_name)
        display_name = tool.get_display_name() if tool else tool_name

        await event_stream.emit_tool_start(tool_name, tool_input, tool_use_id, display_name)

        # Request user confirmation for mutating operations.
        if tool_name in self.CONFIRMATION_TOOLS:
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
                    "tool_use_id": tool_use_id,
                    # Blocking elicitation (already resolved in-tool) — never
                    # triggers the deferred break. Stamped for consistency.
                    "kind": getattr(tool, "kind", "tool") if tool else "tool",
                    "elicit_mode": getattr(tool, "elicit_mode", "deferred") if tool else "deferred",
                    "elicited": False,
                    "elicit_expects": "single",
                }
                return result_block, log_entry

        result = await self._execute_tool(
            tool_name, tool_input, session,
            event_stream=event_stream, tool_use_id=tool_use_id,
        )
        tool_content = result.to_tool_result_content()

        # Use a short display summary for the SSE event — the UI only
        # shows 80 chars anyway and very large payloads (e.g. full page
        # trees) can fragment SSE lines and stall the spinner.
        display_summary = result.summary or result.error or tool_content

        await event_stream.emit_tool_result(tool_name, result.success, display_summary, tool_use_id)

        # audience: a tool whose summary targets the user ("user"/"both") has it
        # posted to chat AND persisted (append to the run-scoped parts the saved
        # turn is built from, so it survives refresh). The model writes only a
        # lead-in (tool-text contract); no de-dup — a rare verbatim echo is OK.
        if result.audience in ("user", "both") and result.success and result.summary:
            await event_stream.emit_text(result.summary)
            assistant_text_parts.append(result.summary)

        # Learning loop: track tool errors for pitfall detection
        if not result.success:
            await self._on_tool_error(tool_name, tool_input, result.error or "Unknown error")

        result_block = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": tool_content,
            "is_error": not result.success,
        }
        # Stamp the elicitation signal on the INTERNAL log_entry (never on
        # result_block, which goes to the Anthropic API). The run loop reads
        # these to decide the turn boundary. data["elicited"] is the runtime
        # signal for tools that only sometimes elicit (decided at call time).
        _elicited = bool(isinstance(result.data, dict) and result.data.get("elicited"))
        log_entry = {
            "tool": tool_name,
            "display_name": display_name,
            "input": tool_input,
            "success": result.success,
            "summary": result.summary or result.error or "",
            "tool_use_id": tool_use_id,
            "kind": getattr(tool, "kind", "tool") if tool else "tool",
            "elicit_mode": getattr(tool, "elicit_mode", "deferred") if tool else "deferred",
            "elicited": _elicited,
            "elicit_expects": (
                (result.data.get("elicit_expects") or "single")
                if _elicited and isinstance(result.data, dict)
                else (getattr(tool, "elicit_expects", "single") if tool else "single")
            ),
            # Domain-agnostic pass-through. A tool may tag its elicitation with
            # the field it fills + a per-option answer map; a subclass can then
            # capture the reply. Inert (None) for every other tool and agent.
            "elicit_field": (result.data.get("elicit_field") if isinstance(result.data, dict) else None),
            "elicit_answers": (result.data.get("elicit_answers") if isinstance(result.data, dict) else None),
            # Generic typed payload an elicitation collects across turns (e.g.
            # the still-missing AssetRequirements). Opaque to core - a subclass owns its
            # shape + (de)serialization. Inert (None) for every other tool.
            "elicit_payload": (result.data.get("elicit_payload") if isinstance(result.data, dict) else None),
            # A success that stored nothing new (kept-noop). The stuck-loop
            # breaker treats it like a failure so a re-send loop the model won't
            # break out of gets the tool quarantined. Generic + inert (False) for
            # every other tool. Domain-agnostic pass-through, same as elicited.
            "no_progress": bool(isinstance(result.data, dict) and result.data.get("no_progress")),
        }
        return result_block, log_entry

    async def _execute_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        session: BaseSession,
        event_stream: AgentEventStream | None = None,
        tool_use_id: str = "",
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
        if event_stream:
            context["event_stream"] = event_stream
        if tool_use_id:
            context["tool_use_id"] = tool_use_id

        try:
            return await tool.execute(tool_input, context)
        except Exception as e:
            logger.exception(f"Tool {tool_name} failed")
            return ToolResult(
                success=False,
                error=f"Tool execution error: {type(e).__name__}: {e}",
            )

    # ── tail-reminder delivery — the injector the _apply_pre_call phase uses to
    # place a build_turn_reminder (Layer 2) at the message tail ──
    @staticmethod
    def _with_tail_reminder(
        messages: list[dict[str, Any]], reminder_text: str,
    ) -> list[dict[str, Any]]:
        """Return a PER-CALL copy of ``messages`` with a fresh ``<system-reminder>``
        text block appended to the tail (the last message's content).

        The reminder is re-derived from live state every turn and must NOT
        accumulate in history — so this copies the list and the last message
        rather than mutating ``session.messages`` (replace-not-append). The
        reminder block carries no ``cache_control``, so it sits after the cached
        prefix. At stream time the last message is always role ``user`` (the user's
        message on turn 1, a tool_result message thereafter), so appending a text
        block is valid for both providers — the OpenAI converter emits it as the
        final user input item; Anthropic accepts text alongside tool_result blocks.
        """
        if not reminder_text:
            return messages
        block = {"type": "text", "text": f"<system-reminder>\n{reminder_text}\n</system-reminder>"}
        out = list(messages)
        if not out:
            return [{"role": "user", "content": [block]}]
        last = dict(out[-1])
        content = last.get("content")
        if isinstance(content, list):
            last["content"] = [*content, block]
        elif isinstance(content, str):
            last["content"] = ([{"type": "text", "text": content}] if content else []) + [block]
        else:
            last["content"] = [block]
        out[-1] = last
        return out

    async def _apply_pre_call(self, session: BaseSession, turn: int) -> list[dict[str, Any]]:
        """Pre-call phase — build the decorated message list for this turn's LLM
        request. The single seam for per-call request decoration: invoke the
        per-turn reminder hook and inject its text at the tail of a PER-CALL copy
        of the messages (fresh every turn, never persisted). Future per-call
        contributions plug in HERE, not in _run_loop. Default hook → "" →
        _with_tail_reminder returns the messages unchanged.
        """
        reminder = await self.build_turn_reminder(session, turn)
        return self._with_tail_reminder(session.get_messages(), reminder)

    # ── setup hook · once per request → folded into the (cached) system prompt ──
    async def build_dynamic_context(self, session: BaseSession) -> str:
        """Layer 1 — per-request context, built ONCE and folded into the
        (cacheable) system prompt.

        Override in subclasses to add agent-specific context that is stable for
        the whole request (e.g. component catalog, app state, learned
        knowledge). Agents whose context is fully per-turn return "" here and
        override build_turn_reminder instead.
        """
        if not session.auth:
            return ""
        return (
            f"Client: {session.auth.client_code}\n"
            f"App: {session.auth.app_code}\n"
        )

    # ── pre-call hook · per turn, before each LLM call → message tail ──
    async def build_turn_reminder(self, session: BaseSession, turn: int) -> str:
        """Pre-call hook (Layer 2) — per-turn ephemeral steering.

        Contract:
        - Cadence: called once per turn, right before the LLM request, via the
          _apply_pre_call phase.
        - Pure read: derive guidance from live state; do not mutate it.
        - Placement: the returned text is injected as a <system-reminder> at the
          messages tail — replace-not-append, never persisted to history, no
          cache_control of its own.
        - Default returns "" → no reminder is injected, messages sent unchanged.

        Override in subclasses that need fresh per-turn guidance ("what's still
        missing, ask this next"). ``turn`` is the 1-based agentic loop turn.
        """
        return ""

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
            if session.auth.path_prefix:
                ctx["path_prefix"] = session.auth.path_prefix
        return ctx

    async def get_pending_suggestions(
        self, session: BaseSession, assistant_text: str = "",
    ) -> dict[str, Any] | None:
        """Return pending suggestion options to show in the UI.

        Override in subclasses to check session context for suggestions
        set by tools like present_options, or to detect choice patterns
        in the assistant response text as a fallback.

        Args:
            session: Active session with context.
            assistant_text: The accumulated assistant response text.

        Returns:
            Dict with 'options' (list of {label, value}) and 'mode'
            ("single" or "multi"), or None.
        """
        return None
