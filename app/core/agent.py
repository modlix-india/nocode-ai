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
from app.core.session import BaseSession, session_app_code
from app.core.context import BaseContext
from app.services import billing
from app.core.builtin_tools import (
    close_builtin_rows, on_builtin_tool_result, on_builtin_tool_use,
)
from app.services.llm_provider import get_llm_provider

logger = logging.getLogger(__name__)



class _ToolBlockAssembler:
    """Assembles streaming `tool_use` blocks, keyed by tool id.

    Providers interleave a PARALLEL batch differently. The OpenAI-compatible
    chat-completions path (DeepSeek, MiniMax) emits every `tool_use_start`
    first, and only afterwards the arguments and ends, each keyed by id. A
    single in-flight slot therefore kept only the LAST call of a batch, paired
    it with the FIRST call's arguments, and silently dropped the rest — which is
    why a 13-conversation bench recorded 147 tool calls across 175 turns with no
    batch ever wider than one, while the same model returns 3 parallel calls
    when asked outside the stream (scripts/probe_parallel_tool_calls.py).

    Anthropic and the non-streaming fallback stream one tool at a time and send
    argument deltas with NO id, so an id-less delta or end applies to the most
    recently started block that has not ended yet.
    """

    def __init__(self) -> None:
        self._open: dict[str, dict[str, Any]] = {}   # insertion-ordered
        self._last: str | None = None
        self._synthetic = 0

    def __bool__(self) -> bool:
        """True while any tool block is still open."""
        return bool(self._open)

    def start(self, tool_id: str, tool_name: str) -> None:
        key = tool_id or ""
        if not key or key in self._open:
            # Providers may omit the id, or repeat one across a batch. Neither
            # may collapse two distinct calls into one block.
            self._synthetic += 1
            key = f"{key}#{self._synthetic}"
        self._open[key] = {
            "type": "tool_use",
            "id": tool_id or key,
            "name": tool_name,
            "input": {},
            "_input_json": "",
        }
        self._last = key

    def _resolve(self, tool_id: str) -> str | None:
        if tool_id and tool_id in self._open:
            return tool_id
        if tool_id:
            # An id we never opened: match on the block's reported id, which can
            # differ from the synthetic key.
            for key, blk in self._open.items():
                if blk.get("id") == tool_id:
                    return key
        if self._last in self._open:
            return self._last
        return next(reversed(list(self._open)), None) if self._open else None

    def delta(self, tool_id: str, fragment: str) -> None:
        key = self._resolve(tool_id)
        if key is None:
            return
        self._open[key]["_input_json"] += fragment

    def end(self, tool_id: str) -> dict[str, Any] | None:
        """Close a block and return it ready for the message, or None."""
        key = self._resolve(tool_id)
        if key is None:
            return None
        block = self._open.pop(key)
        if self._last == key:
            self._last = next(reversed(list(self._open)), None) if self._open else None
        raw = block.pop("_input_json", "") or "{}"
        try:
            block["input"] = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            block["input"] = {}
        return block

    def reset(self) -> None:
        self._open.clear()
        self._last = None


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

    # Meta-tools whose schemas the LLM always sees in full (they're the entry
    # point to the deferred-schema dance — gating them would deadlock).
    _META_TOOL_NAMES: frozenset[str] = frozenset({"search_tools", "get_tool_schema"})

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
        defer_schemas: bool = False,
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

        # Tool surface strategy — three mutually exclusive modes:
        #   1. router_tool  → LLM sees only `execute`; we unwrap
        #                       execute(tool=X, params={}) → real call at
        #                       dispatch (_run_tool_block).
        #   2. defer_schemas → LLM sees every tool's name + description with
        #                       EMPTY parameters; full schema fetched on demand
        #                       via get_tool_schema. First call to a tool
        #                       whose schema hasn't been fetched returns a
        #                       synthetic "here's the schema, retry" result.
        #                       Meta tools (search_tools, get_tool_schema) keep
        #                       their full schemas so the dance has an entry
        #                       point.
        #   3. otherwise     → All tools advertise full schemas (legacy).
        self._router_tool_name = router_tool.name if router_tool else None
        self._defer_schemas = bool(defer_schemas) and not router_tool
        if router_tool:
            self._anthropic_tools = [router_tool.to_anthropic_tool()]
        elif self._defer_schemas:
            self._anthropic_tools = [self._tool_to_advertised_schema(t) for t in tools]
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

    def _tool_to_advertised_schema(self, tool: ToolDefinition) -> dict[str, Any]:
        """Anthropic-shaped tool schema for the deferred-tool surface.

        Meta tools keep their full schemas (so the LLM can call them without
        a prior fetch). Everything else gets advertised with name +
        description + empty input_schema; the full parameters arrive only
        after the LLM calls get_tool_schema(name) — at which point we mark
        it on session.context["fetched_schemas"] and the next invocation
        dispatches normally.
        """
        if tool.name in self._META_TOOL_NAMES:
            return tool.to_anthropic_tool()
        if tool.builtin_spec:
            # Built-in provider tools (web_search etc.) are passed through.
            return tool.to_anthropic_tool()
        return {
            "name": tool.name,
            "description": (tool.description or "").split("\n", 1)[0],
            "input_schema": {"type": "object", "properties": {}},
        }

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

        # Billing — AI is a metered, gated action. Gate this turn against the
        # consumer's wallet before any LLM call. Each LLM call is charged
        # immediately (below), so there is no end-of-turn settlement to snapshot.
        # check_serving_status fails open.
        if not await billing.check_serving_status(session.auth):
            out_msg = "You're out of tokens. Top up your wallet to keep using AI."
            await event_stream.emit_error(out_msg)
            await session.persist_turn(user_message, out_msg, None)
            await session.complete()
            await event_stream.emit_done(session_id=session.session_id, usage=session.get_usage_summary())
            return

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

            # Keep the conversation inside the context window. Nothing else does:
            # `context_management` is an Anthropic-only server-side beta, it is not
            # configured here, and the OpenAI-compatible providers ignore the
            # parameter — so on DeepSeek the history simply grew until the Chit
            # Fund run sat at context_percent 100 and stopped with no summary.
            # Below the threshold this is a single cheap size check per turn.
            # Local import to match the rest of this module: app.config is
            # imported lazily here because agent.py loads before it on some
            # boot paths.
            from app.config import settings as _cfg  # noqa: PLC0415

            session.elide_old_tool_results(
                keep_recent_turns=_cfg.AGENT_HISTORY_KEEP_RECENT_TURNS,
                over_chars=_cfg.AGENT_HISTORY_ELIDE_OVER_CHARS,
                min_result_chars=_cfg.AGENT_HISTORY_ELIDE_MIN_RESULT_CHARS,
                keep_images_turns=_cfg.AGENT_HISTORY_KEEP_IMAGES_TURNS,
            )

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
            # Withdraw quarantined + withheld tools — filtered COPY, never
            # mutate self._anthropic_tools.
            withheld = self.withheld_tool_names(session) | quarantined
            call_tools = (
                [t for t in self._anthropic_tools if t.get("name") not in withheld]
                if withheld else None
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

            # Billing — charge this LLM call's usage immediately. Best-effort;
            # security weights nothing further (weighting is done in billing.py),
            # debits allow-negative, and is idempotent per requestId.
            await billing.charge_llm_call(
                session.auth, usage, resolved_model, request_id, session.session_id)

            # Thinking-mode providers (DeepSeek V4 Pro, MiniMax M3) stash
            # interleaved chain-of-thought in usage["reasoning_content"]. The API
            # requires this text to be passed back on every follow-up turn —
            # `append_assistant_message` stores it as `_reasoning_content` and the
            # provider re-emits it. Pop here so it doesn't leak into the persisted
            # usage record.
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

            # Same-document writes in one batch must not race. Before the
            # parallel-tool-assembly fix a batch collapsed to a single call, so
            # this could never happen; now that batches really do dispatch
            # concurrently, two writes to one page would both fetch it, both
            # mutate their own copy and both save, losing an edit silently.
            # The persona tells the model not to do this; this is the guarantee.
            write_collision = self._batch_write_collision(tool_use_blocks)
            if write_collision:
                logger.warning(
                    "serialising turn %d batch: %s all rewrite %s; parallel saves "
                    "would drop an edit", turn,
                    [tb.get("name", "?") for tb in tool_use_blocks], write_collision,
                )

            run_serial = (
                len(tool_use_blocks) == 1
                or (self.force_serial_on_elicitation and batch_has_elicitation)
                or bool(write_collision)
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

        # Persist the turn summary, tool call log, and context. Cap the summary
        # well under the ASSISTANT_SUMMARY column width (TEXT, ~64KB): a verbose
        # M3 turn can otherwise overflow it, failing the whole turn upsert
        # (MySQL error 1406) and silently dropping conversation history.
        assistant_summary = "".join(assistant_text_parts) if assistant_text_parts else ""
        if len(assistant_summary) > 60000:
            assistant_summary = assistant_summary[:60000] + "\n…[summary truncated]"
        await session.persist_turn(user_message, assistant_summary, tool_call_log or None, model_used)
        await session.save_context()

        # Lore: accumulate what was asked and what happened, so the app's
        # knowledge grows without anyone remembering to write it down.
        await self._observe_to_lore(session, user_message, assistant_summary)

        # (AI billing is charged per LLM call, immediately, inside the loop above.)

        # Emit pending suggestions (e.g. quick reply buttons) if any
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
        tool_blocks = _ToolBlockAssembler()
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
                    tool_blocks.start(chunk.tool_id, chunk.tool_name)

                elif chunk.type == "tool_input_delta":
                    tool_blocks.delta(chunk.tool_id, chunk.tool_input_json)

                elif chunk.type == "tool_use_end":
                    finished = tool_blocks.end(chunk.tool_id)
                    if finished is not None:
                        content_blocks.append(finished)
                        tool_use_blocks.append(finished)

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
                        tool_blocks.reset()

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

        # Remember which object the agent is working on, so the next turn can be
        # handed what is known about it. Best-effort; a missed focus costs
        # nothing, a wrong one would waste the reminder budget.
        try:
            from app.services.lore import context as _lore_ctx
            _lore_ctx.note_focus(session, tool_name, tool_input)
        except Exception:  # noqa: BLE001
            pass

        await event_stream.emit_tool_start(tool_name, tool_input, tool_use_id, display_name)

        # Request user confirmation for mutating operations.
        # Headless/harness callers set session.context["auto_confirm"]=True to
        # pre-approve mutations: skip the SSE pause (which would otherwise block
        # on a /confirm resolver that no headless client provides) AND stamp
        # confirmed=true so the in-tool gate (e.g. theme creation in
        # _handlers.py) passes too. Interactive UI sessions leave auto_confirm
        # unset and keep the normal confirmation flow.
        if tool_name in self.CONFIRMATION_TOOLS and session.context.get("auto_confirm"):
            # Stamp only when the tool declares `confirmed` (create/update read
            # it for their in-tool gate). copy/delete never read it, and an
            # undeclared key would now be refused by _reject_unknown_params,
            # turning every headless call to them into a rejection.
            declares_confirmed = tool is not None and any(p.name == "confirmed" for p in tool.parameters)
            if declares_confirmed and isinstance(tool_input, dict):
                tool_input.setdefault("confirmed", True)
        elif tool_name in self.CONFIRMATION_TOOLS:
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

        # Lore: an edit is the strongest evidence a session produces. Recorded
        # here rather than inside ~190 write tools, so nothing has to remember.
        await self._observe_edit_to_lore(session, tool_name, tool_input, result)

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

        # Multimodal tool_result: when the active provider accepts image
        # content blocks inside `tool_result.content` (Anthropic does), forward
        # any screenshot bytes the tool produced as actual image blocks. Without
        # this the agent on a vision-capable provider only sees a text summary
        # like "PNG in result.data['image_base64']" — useless. Other providers
        # keep the plain-string content path.
        result_content: Any = tool_content
        try:
            from app.services.llm_provider import get_llm_provider as _get_llm_provider
            _provider = _get_llm_provider(self._provider_name)
            if getattr(_provider, "supports_image_in_tool_result", False):
                image_blocks = result.extract_anthropic_image_blocks()
                if image_blocks:
                    result_content = [
                        {"type": "text", "text": tool_content},
                        *image_blocks,
                    ]
        except Exception:  # noqa: BLE001
            pass  # best-effort; fall back to plain text on any provider lookup error

        result_block = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": result_content,
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
        # `progress(msg)` lets a long-running tool stream status lines back
        # to the UI as `tool_update` SSE frames. Fire-and-forget so tool
        # bodies stay synchronous (`progress("...")`, not `await progress(...)`).
        # Tools opt in with `progress = context.get("progress") or (lambda m: None)`.
        if event_stream and tool_use_id:
            def _progress(msg: str) -> None:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(event_stream.emit_tool_update(tool_use_id, str(msg)))
                except Exception:  # noqa: BLE001
                    pass
            context["progress"] = _progress
        else:
            context["progress"] = lambda _m: None

        # Phase 3b: deferred-schema gate. When defer_schemas is on and the LLM
        # calls a non-meta tool whose full schema hasn't been fetched yet, the
        # arguments are checked against the declared schema: a well-formed call
        # dispatches immediately (and is marked fetched), a malformed one gets a
        # synthetic ToolResult carrying the violations + the schema inline, and
        # the LLM re-calls with valid args.
        gate = self._gate_deferred_dispatch(tool_name, tool, context, tool_input)
        if gate is not None:
            return gate

        rejected = self._reject_unknown_params(tool, tool_input)
        if rejected is not None:
            self._remember_failed_call(context, tool_name, tool_input)
            return rejected

        repeat = self._reject_repeat_of_failed_call(context, tool_name, tool_input)
        if repeat is not None:
            return repeat

        try:
            result = await tool.execute(tool_input, context)
        except Exception as e:
            logger.exception(f"Tool {tool_name} failed")
            return ToolResult(
                success=False,
                error=f"Tool execution error: {type(e).__name__}: {e}",
            )

        try:
            self.note_tool_outcome(tool_name, tool_input, result, session)
        except Exception:  # noqa: BLE001
            # Bookkeeping only. A hook defect must not fail a call that worked.
            logger.exception(f"note_tool_outcome failed for {tool_name}")

        if not result.success:
            try:
                note = self.annotate_tool_error(tool_name, tool_input, result, session)
            except Exception:  # noqa: BLE001
                logger.exception(f"annotate_tool_error failed for {tool_name}")
                note = None
            if note and note not in (result.error or ""):
                result.error = f"{result.error or ''}{note}"

        # If the dispatched tool was get_tool_schema, the LLM has just
        # fetched a schema — meta_tools' execute already marked it on
        # ctx["fetched_schemas"] (which is aliased to session.context), so
        # the next call to that tool will pass the gate.
        return result

    _FAILED_CALLS_KEY = "_failed_call_signatures"

    @staticmethod
    def _call_signature(tool_name: str, tool_input: Any) -> str | None:
        """Stable signature for a (tool, args) pair, or None if unhashable."""
        try:
            return f"{tool_name}:{json.dumps(tool_input, sort_keys=True, default=str)}"
        except Exception:  # noqa: BLE001
            return None

    @classmethod
    def _remember_failed_call(cls, context: dict, tool_name: str, tool_input: Any) -> None:
        sig = cls._call_signature(tool_name, tool_input)
        if sig is None:
            return
        seen = context.setdefault(cls._FAILED_CALLS_KEY, {})
        seen[sig] = seen.get(sig, 0) + 1

    @classmethod
    def _reject_repeat_of_failed_call(
        cls, context: dict, tool_name: str, tool_input: Any
    ) -> ToolResult | None:
        """Refuse a call byte-identical to one already rejected this session.

        Re-sending the exact same arguments cannot produce a different outcome,
        but models do it anyway: one build run repeated an identical rejected
        `get_page` five times, another repeated `get_tool_schema` and
        `lore_brief` twice each. Each repeat costs a full turn and teaches the
        model nothing. Say plainly that the arguments are unchanged so the next
        attempt has to differ.
        """
        sig = cls._call_signature(tool_name, tool_input)
        if sig is None:
            return None
        seen = context.get(cls._FAILED_CALLS_KEY) or {}
        if sig not in seen:
            return None
        return ToolResult(
            success=False,
            error=(
                f"These exact arguments to '{tool_name}' were already rejected this "
                f"session, so re-sending them cannot succeed. Nothing was executed. "
                f"Change the arguments, or call a different tool. If you are unsure "
                f"of the parameter names, the rejection above lists the valid ones."
            ),
        )

    @staticmethod
    def _reject_unknown_params(tool: ToolDefinition, tool_input: Any) -> ToolResult | None:
        """Refuse top-level argument names the tool does not declare.

        Tools read params by name, so an undeclared key is silently ignored.
        That is how create_role swallowed `app_code` in the Chit Fund run and
        produced client-scoped roles behind app-scoped page gates. Judged only
        when the tool declares parameters; a tool that intentionally accepts
        free-form input opts out with `allow_unknown_params=True`.
        """
        if not isinstance(tool_input, dict) or not tool.parameters:
            return None
        if getattr(tool, "allow_unknown_params", False):
            return None
        declared = [p.name for p in tool.parameters]
        unknown = [k for k in tool_input if k not in declared]
        if not unknown:
            return None
        import difflib
        described = []
        for k in unknown:
            close = difflib.get_close_matches(k, declared, n=1, cutoff=0.6)
            described.append(f"'{k}'" + (f" (did you mean '{close[0]}'?)" if close else ""))
        return ToolResult(
            success=False,
            error=(
                f"Unknown parameter(s) for {tool.name}: {', '.join(described)}. "
                f"Valid parameters: {', '.join(declared)}. Nothing was executed; "
                "re-call with only valid parameter names."
            ),
        )

    # JSON-Schema type name → the Python types that satisfy it. `bool` is
    # excluded from the numeric entries on purpose: in Python `True` is an
    # `int`, and letting a boolean through as a number is how a truthy flag
    # silently becomes a count.
    _JSON_TYPES: dict[str, tuple[type, ...]] = {
        "string": (str,),
        "integer": (int,),
        "number": (int, float),
        "boolean": (bool,),
        "array": (list, tuple),
        "object": (dict,),
    }

    @classmethod
    def _schema_violations(cls, tool: ToolDefinition, tool_input: Any) -> list[str]:
        """Why ``tool_input`` fails ``tool``'s declared parameters. [] = it passes.

        Checks argument names, required-ness, declared types and enums. This is
        a structural check, not a semantic one — it cannot tell a correct
        page_name from a wrong one, only a call that is shaped like a valid call
        from one that is not.
        """
        if not tool.parameters:
            # Nothing declared to check against; treat any input as satisfying.
            return []
        if not isinstance(tool_input, dict):
            return [f"expected an object of arguments, got {type(tool_input).__name__}"]

        declared = {p.name: p for p in tool.parameters}
        problems: list[str] = []

        if not getattr(tool, "allow_unknown_params", False):
            problems.extend(
                f"unknown parameter '{k}'" for k in tool_input if k not in declared
            )

        for param in tool.parameters:
            if param.name not in tool_input:
                if param.required and param.default is None:
                    problems.append(f"missing required parameter '{param.name}'")
                continue
            value = tool_input[param.name]
            if value is None:
                if param.required:
                    problems.append(f"'{param.name}' is required but was null")
                continue
            allowed = cls._JSON_TYPES.get(param.type)
            if allowed:
                # The bool check comes FIRST and outside the isinstance test:
                # `isinstance(True, int)` is True in Python, so a boolean sails
                # through an int/float check unless it is rejected up front.
                if param.type in ("integer", "number") and isinstance(value, bool):
                    problems.append(f"'{param.name}' expects {param.type}, got boolean")
                elif not isinstance(value, allowed):
                    problems.append(
                        f"'{param.name}' expects {param.type}, got {type(value).__name__}"
                    )
            if param.enum and isinstance(value, str) and value not in param.enum:
                problems.append(
                    f"'{param.name}' must be one of {param.enum}, got '{value}'"
                )

        return problems

    def note_tool_outcome(
        self,
        tool_name: str,
        tool_input: Any,
        result: ToolResult,
        session: BaseSession,
    ) -> None:
        """Observe a completed tool call to update cross-cutting session state.

        Called once per dispatch, after the tool returns. The point is to let an
        agent learn something from an outcome that no individual tool should have
        to report: a hundred tools should not each remember to write down which
        app the work actually landed in.

        Default is a no-op — core holds no opinion about another layer's tools.
        Exceptions raised here are swallowed by the caller: a bookkeeping hook
        must never turn a successful tool call into a failure.
        """

    def annotate_tool_error(
        self,
        tool_name: str,
        tool_input: Any,
        result: ToolResult,
        session: BaseSession,
    ) -> str | None:
        """Extra sentence to append to a failed tool's error, or None.

        The failure text a tool writes knows only what that tool was asked to
        do. Session-level knowledge that would explain the failure ("you have
        been writing to a different app all along") lives up here, so this is
        where it gets attached.

        Default None. Like `note_tool_outcome`, exceptions are swallowed.
        """
        return None

    def write_conflict_key(self, tool_name: str, tool_input: Any) -> str | None:
        """Identity of the document this call will read-modify-write, or None.

        Two calls in one parallel batch that return the SAME key would both fetch
        the document, both mutate their own copy and both save it, so the later
        save silently discards the earlier edit. `_batch_write_collision` uses
        this to fall back to serial dispatch for exactly those batches.

        Default None means "this agent does not know which of its tools mutate",
        so nothing is serialised and behaviour is unchanged. An agent that knows
        its tool surface overrides this; core deliberately holds no table of
        another layer's tools.
        """
        return None

    def _batch_write_collision(self, tool_use_blocks: list[dict[str, Any]]) -> str | None:
        """The first document two calls in this batch would both rewrite, or None.

        Read-only calls (key None) never collide, so a batch of six reads plus one
        write still runs fully parallel. Only a genuine same-document pair forces
        serial dispatch, which keeps the batching win on the case the persona
        actively encourages: patches to DIFFERENT pages in one message.
        """
        seen: set[str] = set()
        for tb in tool_use_blocks:
            key = self.write_conflict_key(tb.get("name") or "", tb.get("input") or {})
            if key is None:
                continue
            if key in seen:
                return key
            seen.add(key)
        return None

    def withheld_tool_names(self, session: BaseSession) -> set[str]:
        """Tool names to leave OUT of this turn's advertised `tools=` payload.

        Default: none. Subclasses override to keep a long tail of rarely-needed
        tools out of the per-request payload, which every turn pays for.

        A withheld tool is not unreachable. `search_tools` searches the full
        registry from `context["tools"]`, not the advertised list, so the LLM
        can still find it by keyword; fetching its schema with
        `get_tool_schema` records it in `session.context["fetched_schemas"]`,
        which an override consults to stop withholding it for the rest of the
        session. Discovery costs one turn and then the tool behaves normally.

        Withholding a tool the model cannot discover would be a silent
        capability loss, so an override MUST leave it listed in the system
        prompt's tool index.
        """
        return set()

    def _gate_deferred_dispatch(
        self,
        tool_name: str,
        tool: ToolDefinition,
        context: dict[str, Any],
        tool_input: Any = None,
    ) -> ToolResult | None:
        """Return a synthetic schema-injection ToolResult, or None to dispatch.

        Conditions for synthesizing instead of dispatching:
          - defer_schemas mode is on
          - The tool is NOT the router and NOT a meta tool
          - The tool's full schema isn't in session.context["fetched_schemas"]
          - AND the arguments the LLM supplied do NOT already satisfy the
            declared schema.

        That last condition is the point. This gate used to fire on the first
        call to a tool unconditionally, without ever looking at the arguments —
        so a model that guessed a simple signature correctly (`list_themes(
        app_code="x")`) still burned a whole round-trip being told a schema it
        had evidently already inferred. The bounce is only worth paying when the
        guess was actually wrong; when the call validates, dispatch it and mark
        the schema fetched, exactly as a successful `get_tool_schema` would.

        This is what makes the `HOT_TOOLS` full-schema list mostly redundant:
        its whole job was dodging a first-call bounce that no longer happens for
        a well-formed call.

        Validation is structural (names, required, types, enums) — it does not
        vouch for the *meaning* of the arguments. It never did: on the retry the
        model re-sends arguments this same check would have accepted, so nothing
        that reaches execution here could not already reach it one turn later.
        """
        if not self._defer_schemas:
            return None
        if tool_name == self._router_tool_name:
            return None
        if tool_name in self._META_TOOL_NAMES:
            return None
        if tool.builtin_spec:
            # Provider-executed built-ins don't go through this path anyway.
            return None
        fetched = context.get("fetched_schemas")
        # Membership check works on both list and set (covers in-flight
        # sessions that still hold the legacy set shape).
        if isinstance(fetched, (list, set)) and tool_name in fetched:
            return None

        def _mark_fetched() -> None:
            if isinstance(fetched, list):
                if tool_name not in fetched:
                    fetched.append(tool_name)
            elif isinstance(fetched, set):
                fetched.add(tool_name)

        violations = self._schema_violations(tool, tool_input)
        if not violations:
            # The model got it right without being shown the schema. Record it
            # as fetched so later calls skip this check too, and let it through.
            _mark_fetched()
            logger.debug("Deferred gate: '%s' validated on first call, dispatching", tool_name)
            return None

        # The guess was wrong — spend the round-trip, and say WHY as well as
        # what the schema is, so the retry is informed rather than a re-guess.
        import json as _json
        anthropic_shape = tool.to_anthropic_tool()
        payload = {
            "name": tool.name,
            "description": tool.description,
            "input_schema": anthropic_shape.get("input_schema", {"type": "object", "properties": {}}),
        }
        _mark_fetched()
        body = (
            f"NOTE: '{tool_name}' was called before its full schema was fetched, "
            "and the arguments did not match it:\n"
            + "\n".join(f"  - {v}" for v in violations)
            + "\n\nThe schema is below and is now marked fetched on this session "
            "— re-call the tool with arguments that match it. Subsequent calls "
            "within this conversation will dispatch immediately.\n\n"
            f"```json\n{_json.dumps(payload, indent=2, default=str)}\n```"
        )
        return ToolResult(success=True, summary=body)

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
        lore_reminder = await self._lore_turn_context(session)
        if lore_reminder:
            reminder = f"{reminder}\n{lore_reminder}" if reminder else lore_reminder
        return self._with_tail_reminder(session.get_messages(), reminder)

    async def _lore_turn_context(self, session: BaseSession) -> str:
        """Small picture: what is known about the object now in focus.

        Composed here rather than inside `build_turn_reminder` so subclasses
        keep that hook pure, and so every agent gets this without opting in.
        Pushed once per subject per session; repeating it every turn would
        spend the whole reminder budget restating what was said three turns ago.
        """
        from app.config import settings as _settings
        if not getattr(_settings, "LORE_ENABLED", True):
            return ""
        try:
            from app.services.lore import context as _lore_ctx
            subject = _lore_ctx.take_unsent_focus(session)
            if not subject:
                return ""
            return await _lore_ctx.small_picture(session, subject)
        except Exception:  # noqa: BLE001 — lore must never break a turn
            logger.debug("lore: turn context skipped", exc_info=True)
            return ""

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

    class _ScopeAuth:
        """Minimal shape lore's access resolver needs."""

        def __init__(self, client_code: str) -> None:
            self.client_code = client_code

    @staticmethod
    def _lore_target(session: BaseSession) -> tuple[str, str] | None:
        """(client_code, app_code) for lore writes, or None if lore is off.

        Reads everything defensively: this runs on every tool call, and a
        session shape it did not expect must yield "no lore", never an
        exception in the middle of somebody's build.
        """
        from app.config import settings as _settings

        if not _settings.LORE_ENABLED:
            return None
        auth = getattr(session, "auth", None)
        client_code = getattr(auth, "client_code", "") if auth else ""
        # Lore is knowledge ABOUT an app, so it belongs to the app the session
        # is actually building, not the one it happened to open from.
        app_code = session_app_code(session)
        if not client_code or not app_code:
            return None
        return client_code, app_code

    @staticmethod
    async def _lore_should_curate(client_code: str, app_code: str) -> bool:
        """Is there enough pending evidence to be worth an LLM curation pass?

        Two triggers, either sufficient: app-wide volume, or depth about one
        subject. The second exists because volume alone is the wrong unit —
        a build touching thirty objects once each has learned less than one
        that reworked a single page eight times, and only the second yields an
        entry worth reading.
        """
        from app.config import settings as _settings
        from app.services.lore import store as _store

        app_threshold = int(_settings.LORE_AUTOCURATE_AT or 0)
        subject_threshold = int(_settings.LORE_AUTOCURATE_SUBJECT_AT or 0)
        if app_threshold <= 0 and subject_threshold <= 0:
            return False

        if app_threshold > 0 and await _store.count_pending(client_code, app_code) >= app_threshold:
            return True
        if subject_threshold > 0:
            subject, count = await _store.busiest_pending_subject(client_code, app_code)
            # "app" is the catch-all bucket, so depth there means nothing.
            if subject and subject != "app" and count >= subject_threshold:
                return True
        return False

    async def _observe_edit_to_lore(
        self, session: BaseSession, tool_name: str, tool_input: Any, result: Any,
    ) -> None:
        """Record a successful definition write as an `edit` observation.

        This is the path that carries real evidence. A chat turn is the agent
        narrating what it believes it did; an edit is what the platform
        actually accepted, and it names the object it happened to.

        Best-effort in exactly the same way as the turn observer: an app being
        built must never fail because its knowledge could not be recorded.
        """
        from app.config import settings as _settings

        if not _settings.LORE_OBSERVE_EDITS:
            return
        target = self._lore_target(session)
        if target is None:
            return
        client_code, app_code = target

        try:
            from app.services.lore import watch as _watch

            fact = _watch.classify(
                tool_name, tool_input,
                summary=getattr(result, "summary", "") or "",
                success=bool(getattr(result, "success", False)),
            )
            if fact is None:
                return

            from app.services.lore import access as _access
            from app.services.lore import ingest as _ingest

            scope = await _access.resolve_scope(self._ScopeAuth(client_code), app_code)
            if not scope.can_write:
                return

            auth = getattr(session, "auth", None)
            await _ingest.from_edit(
                client_code, app_code,
                object_type=fact.object_type,
                object_name=fact.object_name,
                action=fact.action,
                subject=fact.subject,
                detail=fact.detail,
                actor=self.name,
                user_id=int(getattr(auth, "user_id", 0) or 0),
                meta={"tool": tool_name, "session_id": session.session_id},
            )
        except Exception:  # noqa: BLE001 — lore must never break a tool call
            logger.debug("lore: edit observation skipped", exc_info=True)

    async def _observe_to_lore(
        self, session: BaseSession, user_message: str, assistant_summary: str,
    ) -> None:
        """Record this turn into the app's lore, and curate when it piles up.

        Entirely best-effort: lore is a nice-to-have that must never affect a
        user's turn, so every failure is swallowed at debug level. The curation
        pass is fired as a background task rather than awaited for the same
        reason — it involves an LLM call and the user is already done.
        """
        from app.config import settings as _settings

        if not (_settings.LORE_ENABLED and _settings.LORE_OBSERVE_CHAT):
            return
        auth = getattr(session, "auth", None)
        client_code = getattr(auth, "client_code", "") if auth else ""
        app_code = session_app_code(session)
        if not client_code or not app_code:
            return

        try:
            from app.services.lore import access as _access
            from app.services.lore import curator as _curator
            from app.services.lore import ingest as _ingest
            from app.services.lore import store as _store

            # Passive accumulation is still accumulation: an observation becomes
            # an entry at the next curation pass, so it needs the same edit
            # access a person would need to write one by hand.
            scope = await _access.resolve_scope(self._ScopeAuth(client_code), app_code)
            if not scope.can_write:
                return

            await _ingest.from_chat_turn(
                client_code, app_code,
                session_id=session.session_id,
                agent_name=self.name,
                user_message=user_message,
                assistant_message=assistant_summary,
                user_id=int(getattr(auth, "user_id", 0) or 0),
            )

            if await self._lore_should_curate(client_code, app_code):
                task = asyncio.create_task(
                    _curator.curate(client_code, app_code, trigger_source=f"turn:{self.name}")
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
        except Exception:  # noqa: BLE001 — lore must never break a turn
            logger.debug("lore: turn observation skipped", exc_info=True)

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
            # AuthContext object — kb_app and call_as_app_user read user_id
            # off it for audit / identity-stamping.
            "auth": session.auth,
            # The full tool catalog — meta_tools (search_tools / get_tool_schema)
            # scope their searches to this list. Re-built each turn from the
            # agent's registry so newly-registered tools are visible.
            "tools": list(self.tools.values()),
            # App-user token resolver — visuals tools (screenshot_page,
            # drive_page) call this to log the headless browser in as one of
            # the customer's end users. Returns the cached token if known,
            # else lazily runs findUserClients + authenticate against
            # session.auth.app_code using credentials passed in the chat
            # request's `app_user.{username, password}`. Raises with a clear
            # hint if neither token nor credentials are available.
            "get_app_user_token": session.get_app_user_token,
            # Session-scoped MUTABLE caches. These MUST be aliased to
            # session.context (via setdefault) so mutations from one tool call
            # survive into the next call within the same conversation —
            # critical for:
            #   * fetched_schemas: tracks which tool schemas have been pulled
            #     via get_tool_schema, gating first-call dispatch (Phase 3b).
            #     LIST (not set) so session.context stays JSON-serializable
            #     for cross-request persistence.
            #   * pending_kb_updates: stashes propose_kb_update payloads
            #     awaiting their commit_kb_update partner.
            "fetched_schemas": session.context.setdefault("fetched_schemas", []),
            "pending_kb_updates": session.context.setdefault("pending_kb_updates", {}),
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
