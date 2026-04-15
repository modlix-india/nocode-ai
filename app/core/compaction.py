"""Auto-compaction engine for long-running agent sessions.

When the conversation context approaches the model's token limit,
this engine summarizes older messages via a separate (cheap/fast) LLM
call and replaces them with a compact summary.  Critical state is
re-injected after compaction so the agent doesn't lose track of
entity names, IDs, section versions, or its current plan.

Mirrors Claude Code's AutoCompact pattern:
1. Trigger at 80% of context limit
2. Summarize old messages (keep last N pairs verbatim)
3. Replace with [COMPACTED SUMMARY] message
4. Re-inject: plan state, definition cache, discovered tools, last tool results

Also exposes a lightweight ``prune_old_tool_results`` function that
PROACTIVELY truncates large tool_result blocks from older turns
BEFORE the context hits the compaction threshold. This keeps per-call
cost low even in short sessions where full compaction never triggers.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.session import BaseSession

logger = logging.getLogger(__name__)


# ── Proactive tool-result pruning ───────────────────────────────────

# A tool_result is considered "large" if its text payload exceeds this.
# 1500 chars ≈ 375 tokens. Below this, pruning saves little and may hurt
# the agent's ability to reason about recent actions.
_LARGE_RESULT_THRESHOLD = 1500

# Keep the most recent N user+assistant round-trips' tool results verbatim.
# Older tool results get truncated to a summary stub.
_KEEP_RECENT_ROUNDTRIPS = 2


def prune_old_tool_results(
    session: BaseSession,
    keep_recent_roundtrips: int = _KEEP_RECENT_ROUNDTRIPS,
    large_threshold: int = _LARGE_RESULT_THRESHOLD,
) -> int:
    """Truncate large tool_result blocks from older turns.

    Walks the session's message history and truncates tool_result
    content in turns older than ``keep_recent_roundtrips``. This is a
    mutating operation — the session's history is modified in place.

    This runs every turn (before the LLM call) so large reads don't
    linger in history forever. The most recent turns keep full detail
    so the agent can still reason about its recent actions.

    Args:
        session: The active session.
        keep_recent_roundtrips: How many recent user→assistant round-trips
            to leave untouched. Each round-trip is a user message +
            assistant reply (which may include multiple tool_use/result
            pairs).
        large_threshold: Only results with text longer than this are
            truncated. Small tool results pass through unchanged.

    Returns:
        The total number of characters saved by truncation. Useful for
        logging.
    """
    messages = session.messages
    if len(messages) < 4:
        return 0

    # Find the cut-off index: everything BEFORE this index is "old" and
    # eligible for pruning. Walk backward from the end, counting user
    # round-trips (non-tool-result user messages). The cutoff is the START
    # of the Kth-most-recent user message — everything before it belongs to
    # even older round-trips and gets pruned.
    roundtrips_seen = 0
    cutoff_idx = 0
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") == "user" and not _is_tool_result_message(msg):
            roundtrips_seen += 1
            if roundtrips_seen == keep_recent_roundtrips:
                cutoff_idx = i
                break

    if cutoff_idx == 0:
        # Not enough history to prune anything
        return 0

    chars_saved = 0
    pruned_count = 0
    for i in range(cutoff_idx):
        msg = messages[i]
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            result = block.get("content", "")
            if isinstance(result, str) and len(result) > large_threshold:
                tool_id = block.get("tool_use_id", "?")[:12]
                original_len = len(result)
                # Keep the first ~200 chars (usually has the summary/status)
                truncated = (
                    f"[tool result from earlier turn, truncated — "
                    f"tool_use_id={tool_id}, original size {original_len} chars]\n"
                    f"{result[:200]}...\n"
                    f"[end truncated]"
                )
                block["content"] = truncated
                chars_saved += original_len - len(truncated)
                pruned_count += 1
            elif isinstance(result, list):
                # Multimodal tool result (rare) — truncate each text block
                for sub in result:
                    if (isinstance(sub, dict) and sub.get("type") == "text"
                            and len(sub.get("text", "")) > large_threshold):
                        original = sub["text"]
                        sub["text"] = (
                            f"[tool result text from earlier turn, truncated — "
                            f"original size {len(original)} chars]\n"
                            f"{original[:200]}..."
                        )
                        chars_saved += len(original) - len(sub["text"])
                        pruned_count += 1

    if pruned_count > 0:
        logger.info(
            "Pruned %d old tool_result(s), saved ~%d chars (~%d tokens) "
            "from conversation history",
            pruned_count, chars_saved, chars_saved // 4,
        )
    return chars_saved


def _is_tool_result_message(msg: dict[str, Any]) -> bool:
    """True if this user message contains tool_result blocks (not a real user msg)."""
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_result"
        for b in content
    )

# Compaction summary prompt — instructs the fast model what to preserve
_COMPACTION_SYSTEM_PROMPT = """\
You are a conversation summarizer for an AI application builder.
Summarize the conversation so far into a compact but complete summary.

You MUST preserve:
- All entity names and IDs (application codes, page names, function names, theme names, etc.)
- All version numbers and section version numbers
- What was built, created, or modified (and in what order)
- What the user asked for and what remains to be done
- Any errors that occurred and how they were resolved
- The current state of the application being built

Be concise but do NOT omit entity names, IDs, or version numbers — the agent needs these to continue working.
Format as a structured summary with sections: Context, Entities, Progress, Next Steps.
"""


class ContextCompactor:
    """Manages context window compaction for long-running sessions."""

    def __init__(
        self,
        context_limit: int = 180_000,
        threshold: float = 0.80,
        post_compact_budget: int = 20_000,
        keep_recent: int = 2,
    ) -> None:
        """
        Args:
            context_limit: Maximum context tokens for the model.
            threshold: Compact when usage exceeds this ratio of context_limit.
            post_compact_budget: Token budget for re-injected state after compaction.
            keep_recent: Number of recent user/assistant round-trips to keep verbatim.
        """
        self.context_limit = context_limit
        self.threshold = threshold
        self.post_compact_budget = post_compact_budget
        self.keep_recent = keep_recent

    def should_compact(self, session: BaseSession) -> bool:
        """Check if the session's context is approaching the limit."""
        estimated = session.estimated_context_tokens()
        limit = int(self.context_limit * self.threshold)
        if estimated > limit:
            logger.info(
                "Compaction needed: ~%d tokens > %d threshold (%.0f%% of %d limit)",
                estimated, limit, estimated / self.context_limit * 100, self.context_limit,
            )
            return True
        return False

    async def compact(
        self,
        session: BaseSession,
        provider: Any,
        definition_cache: Any = None,
    ) -> None:
        """Compact the conversation by summarizing old messages.

        Args:
            session: The active session to compact.
            provider: LLM provider instance for the summarization call.
            definition_cache: Optional DefinitionCache for state re-injection.
        """
        messages = session.messages
        if len(messages) < 4:
            logger.info("Too few messages to compact (%d), skipping", len(messages))
            return

        # Split into old messages (to summarize) and recent (to keep)
        keep_count = self.keep_recent * 2  # each round-trip = user + assistant
        if len(messages) <= keep_count + 2:
            logger.info("Not enough messages to compact (need >%d, have %d)", keep_count + 2, len(messages))
            return

        old_messages = messages[:-keep_count] if keep_count > 0 else messages[:]
        recent_messages = messages[-keep_count:] if keep_count > 0 else []

        # Build the conversation text to summarize
        conversation_text = _format_messages_for_summary(old_messages)

        logger.info(
            "Compacting %d old messages (~%d chars) while keeping %d recent messages",
            len(old_messages), len(conversation_text), len(recent_messages),
        )

        # Call the fast model to generate a summary
        try:
            summary = await _generate_summary(provider, conversation_text)
        except Exception as e:
            logger.error("Compaction summarization failed: %s", e)
            return

        # Build post-compaction re-injection content
        reinjection = _build_reinjection(session, definition_cache, recent_messages)

        # Replace messages: [COMPACTED SUMMARY] + re-injection + recent messages
        compact_message = {
            "role": "user",
            "content": (
                f"[COMPACTED CONVERSATION SUMMARY]\n\n{summary}"
                + (f"\n\n[CURRENT STATE]\n\n{reinjection}" if reinjection else "")
            ),
        }
        ack_message = {
            "role": "assistant",
            "content": [{"type": "text", "text": (
                "Understood. I have the conversation summary and current state. "
                "I'll continue from where we left off."
            )}],
        }

        session.messages = [compact_message, ack_message] + recent_messages

        new_tokens = session.estimated_context_tokens()
        logger.info(
            "Compaction complete: %d messages → %d messages, ~%d tokens",
            len(old_messages) + len(recent_messages), len(session.messages), new_tokens,
        )


def _format_messages_for_summary(messages: list[dict[str, Any]]) -> str:
    """Format messages into a text block for the summarization LLM."""
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "?").upper()
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(f"[{role}]: {content}")
        elif isinstance(content, list):
            text_parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_name = block.get("name", "?")
                    tool_input = block.get("input", {})
                    # Compact tool input to avoid blowing up the summary
                    input_str = str(tool_input)
                    if len(input_str) > 200:
                        input_str = input_str[:200] + "..."
                    text_parts.append(f"[Tool: {tool_name}({input_str})]")
                elif block.get("type") == "tool_result":
                    result_content = block.get("content", "")
                    if isinstance(result_content, str) and len(result_content) > 200:
                        result_content = result_content[:200] + "..."
                    text_parts.append(f"[Result: {result_content}]")
            if text_parts:
                parts.append(f"[{role}]: " + " ".join(text_parts))
    return "\n\n".join(parts)


async def _generate_summary(provider: Any, conversation_text: str) -> str:
    """Call the fast LLM to summarize the conversation."""
    from app.config import settings

    messages = [{"role": "user", "content": conversation_text}]
    response = await provider.create_completion(
        system_prompt=_COMPACTION_SYSTEM_PROMPT,
        messages=messages,
        model_tier=settings.COMPACTION_MODEL_TIER,
        max_tokens=4096,
        use_cache=False,
    )
    return response.get("content", "")


def _build_reinjection(
    session: BaseSession,
    definition_cache: Any,
    recent_messages: list[dict[str, Any]],
) -> str:
    """Build the post-compaction re-injection content.

    Includes: plan state, definition cache summary, discovered tools,
    and the last few tool results.
    """
    parts: list[str] = []

    # 1. Plan state
    plan = session.context.get("plan")
    if plan:
        import json
        parts.append(f"Current plan:\n{json.dumps(plan, indent=2)}")

    # 2. Definition cache summary
    if definition_cache is not None:
        try:
            cache_summary = definition_cache.to_compact_summary()
            if cache_summary:
                parts.append(f"Known entities:\n{cache_summary}")
        except Exception:
            pass

    # 3. Discovered tools
    discovered = session.context.get("discovered_tools", [])
    if discovered:
        parts.append(f"Discovered tools (available for use): {', '.join(discovered)}")

    # 4. Last tool results from recent messages
    tool_results: list[str] = []
    for msg in recent_messages:
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                result_text = block.get("content", "")
                if isinstance(result_text, str) and result_text:
                    tool_results.append(result_text[:500])
    if tool_results:
        parts.append("Recent tool results:\n" + "\n---\n".join(tool_results[-3:]))

    return "\n\n".join(parts)
