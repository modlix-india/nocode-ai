from __future__ import annotations

"""
LLM Provider abstraction for supporting multiple LLM backends.

Supports:
- Anthropic (Claude): claude-haiku-4-5, claude-sonnet-4
- OpenAI (GPT): gpt-4o-mini, gpt-4o
- DeepSeek: deepseek-chat (V3)

Usage:
    from app.services.llm_provider import get_llm_provider
    
    provider = get_llm_provider()
    response = await provider.create_completion(
        system_prompt="You are a helpful assistant",
        messages=[{"role": "user", "content": "Hello"}],
        model_tier="balanced"
    )
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, AsyncIterator
import logging
import asyncio

logger = logging.getLogger(__name__)


def _safe_parse_tool_args(raw_args: str | None, tool_name: str, model: str) -> dict:
    """Parse tool call arguments JSON with repair for malformed output.

    LLMs (especially DeepSeek) sometimes produce invalid JSON in tool
    arguments: trailing commas, truncated strings, unescaped characters.
    This function attempts increasingly aggressive repairs before falling
    back to empty args.
    """
    import json as json_lib
    import re

    raw = raw_args or "{}"

    # 1. Try as-is
    try:
        return json_lib.loads(raw)
    except json_lib.JSONDecodeError:
        pass

    logger.warning("Malformed tool args from %s for %s (len=%d), attempting repair",
                   model, tool_name, len(raw))

    # 2. Strip trailing commas before } or ]
    try:
        return json_lib.loads(re.sub(r',\s*([}\]])', r'\1', raw))
    except json_lib.JSONDecodeError:
        pass

    # 3. Truncate at last valid closing brace and try
    last_brace = raw.rfind('}')
    if last_brace > 0:
        try:
            return json_lib.loads(raw[:last_brace + 1])
        except json_lib.JSONDecodeError:
            pass

    # 4. Try closing unclosed braces/brackets (truncated response)
    try:
        repaired = re.sub(r',\s*([}\]])', r'\1', raw)
        # Remove any trailing incomplete string value
        repaired = re.sub(r',\s*"[^"]*$', '', repaired)
        # Count unclosed braces/brackets and close them
        open_braces = repaired.count('{') - repaired.count('}')
        open_brackets = repaired.count('[') - repaired.count(']')
        if open_braces > 0 or open_brackets > 0:
            repaired += ']' * max(0, open_brackets) + '}' * max(0, open_braces)
            return json_lib.loads(repaired)
    except json_lib.JSONDecodeError:
        pass

    # 5. Try wrapping in braces if it looks like bare key-value pairs
    if not raw.strip().startswith('{'):
        try:
            return json_lib.loads('{' + raw + '}')
        except json_lib.JSONDecodeError:
            pass

    logger.error("Could not repair tool args for %s (first 300 chars): %s", tool_name, raw[:300])
    return {}


class LLMProvider(ABC):
    """Abstract base class for LLM providers"""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name for logging"""
        pass
    
    @abstractmethod
    def get_model(self, tier: str) -> str:
        """
        Get the model name for a given tier.
        
        Args:
            tier: "fast" or "balanced"
        
        Returns:
            Model name string
        """
        pass
    
    @abstractmethod
    async def create_completion(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        model_tier: str = "balanced",
        max_tokens: int = 8192,
        use_cache: bool = True
    ) -> Dict[str, Any]:
        """
        Create a completion using the LLM.
        
        Args:
            system_prompt: System prompt text
            messages: List of message dicts with role and content
            model_tier: "fast" or "balanced"
            max_tokens: Maximum tokens in response
            use_cache: Whether to use prompt caching (if supported)
        
        Returns:
            Dict with:
            - content: Response text
            - usage: Token usage info
        """
        pass
    
    @abstractmethod
    def supports_vision(self) -> bool:
        """Whether this provider supports vision/image inputs"""
        pass
    
    @abstractmethod
    def supports_prompt_caching(self) -> bool:
        """Whether this provider supports prompt caching"""
        pass
    
    @abstractmethod
    async def create_completion_with_tools(
        self,
        system_prompt: Any,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        model_tier: str = "balanced",
        max_tokens: int = 16384,
    ) -> Dict[str, Any]:
        """
        Create a completion with tool-use support (for agentic loops).

        Args:
            system_prompt: System prompt — either a string or a list of
                Anthropic content blocks (with cache_control).
            messages: Conversation history in Anthropic format.
            tools: List of tool definitions in Anthropic format
                (from ToolDefinition.to_anthropic_tool()).
            model_tier: "fast" or "balanced"
            max_tokens: Maximum tokens in response

        Returns:
            Dict with:
            - content: List of content blocks (TextBlock, ToolUseBlock dicts)
            - usage: Token usage info
            - model: Model name used
            - stop_reason: "end_turn" or "tool_use"
        """
        pass

    async def stream_completion_with_tools(
        self,
        system_prompt: Any,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        model_tier: str = "balanced",
        max_tokens: int = 16384,
        context_management: dict | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a completion with tool-use support.

        Yields StreamChunk objects as they arrive. Override in subclasses
        for native streaming. Default: falls back to non-streaming call
        and yields the complete response as chunks.
        """
        response = await self.create_completion_with_tools(
            system_prompt, messages, tools, model_tier, max_tokens,
        )
        # Emit text and tool blocks as chunks from the complete response
        for block in response.get("content", []):
            if block.get("type") == "text" and block.get("text"):
                yield StreamChunk(type="text_delta", text=block["text"])
            elif block.get("type") == "tool_use":
                import json as json_lib
                yield StreamChunk(
                    type="tool_use_start",
                    tool_name=block["name"],
                    tool_id=block["id"],
                )
                yield StreamChunk(
                    type="tool_input_delta",
                    tool_input_json=json_lib.dumps(block["input"]),
                )
                yield StreamChunk(type="tool_use_end", tool_id=block["id"])
        yield StreamChunk(
            type="done",
            stop_reason=response.get("stop_reason", "end_turn"),
            usage=response.get("usage", {}),
        )

    def format_image_content(self, base64_image: str, media_type: str = "image/png") -> Dict[str, Any]:
        """
        Format image content for the provider's message format.

        Args:
            base64_image: Base64 encoded image data
            media_type: MIME type of the image

        Returns:
            Provider-specific image content dict
        """
        raise NotImplementedError("Vision not supported by this provider")


class AnthropicProvider(LLMProvider):
    """Anthropic Claude provider"""

    def __init__(self):
        import anthropic
        from app.config import settings

        self.client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        self.settings = settings
        self._models = {
            "fast": settings.CLAUDE_HAIKU,
            "balanced": settings.CLAUDE_SONNET
        }

    @property
    def name(self) -> str:
        return "Anthropic"

    def get_model(self, tier: str) -> str:
        # Known tier → mapped model; otherwise treat tier as a direct model name
        return self._models.get(tier, tier)

    # Map from Anthropic server-tool spec ``type`` to the beta header that
    # must be set when that tool is declared. Add new entries here when
    # Anthropic ships additional server tools behind betas.
    _BETA_HEADERS_BY_TOOL_TYPE: Dict[str, str] = {
        "web_fetch_20250910": "web-fetch-2025-09-10",
    }

    def _convert_tools(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert tool schemas to Anthropic's ``tools`` format.

        - Function tools (no ``__builtin__`` marker): passed through as-is.
        - Builtin tools marked for Anthropic: ``spec`` is forwarded verbatim
          (e.g. ``{"type": "web_search_20250305", "name": "web_search",
          "max_uses": 10}``). The ``provider`` key inside spec is stripped —
          it's our routing hint, not part of Anthropic's API.
        - Builtin tools for other providers: dropped.
        """
        out: List[Dict[str, Any]] = []
        for tool in tools:
            if tool.get("__builtin__"):
                if tool.get("provider") != "anthropic":
                    continue
                spec = dict(tool.get("spec") or {})
                spec.pop("provider", None)
                if spec:
                    out.append(spec)
                continue
            out.append(tool)
        return out

    def _beta_headers_for(self, converted_tools: List[Dict[str, Any]]) -> Dict[str, str]:
        """Return ``extra_headers`` for ``messages.create`` / ``messages.stream``.

        Iterates converted (Anthropic-native) tool specs and emits the
        ``anthropic-beta`` header for any that require it (e.g. web_fetch).
        Returns an empty dict when no beta tools are in use.
        """
        betas: List[str] = []
        for spec in converted_tools:
            beta = self._BETA_HEADERS_BY_TOOL_TYPE.get(spec.get("type", ""))
            if beta and beta not in betas:
                betas.append(beta)
        if not betas:
            return {}
        return {"anthropic-beta": ",".join(betas)}

    async def create_completion(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        model_tier: str = "balanced",
        max_tokens: int = 8192,
        use_cache: bool = True
    ) -> Dict[str, Any]:
        """Create completion using Claude API"""
        model = self.get_model(model_tier)
        
        # Build system prompt with caching if enabled
        if use_cache and self.settings.PROMPT_CACHING_ENABLED:
            system = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"}
                }
            ]
        else:
            system = system_prompt
        
        # Run synchronous API call in thread pool
        response = await asyncio.to_thread(
            self.client.messages.create,
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages
        )
        
        return {
            "content": response.content[0].text,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
                "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0)
            },
            "model": model,
            "stop_reason": response.stop_reason
        }
    
    async def create_completion_with_tools(
        self,
        system_prompt: Any,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        model_tier: str = "balanced",
        max_tokens: int = 16384,
    ) -> Dict[str, Any]:
        """Create completion with tool-use via Claude API.

        system_prompt can be a string or a list of content blocks
        (with cache_control for prompt caching).
        """
        model = self.get_model(model_tier)

        # Translate builtin markers into Anthropic's tool format; drop others.
        tools = self._convert_tools(tools)
        extra_headers = self._beta_headers_for(tools)

        # If system_prompt is a plain string, wrap with caching if enabled
        if isinstance(system_prompt, str):
            if self.settings.PROMPT_CACHING_ENABLED:
                system = [
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                system = system_prompt
        else:
            # Already a list of content blocks (caller handles caching)
            system = system_prompt

        create_kwargs: Dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=tools,
        )
        if extra_headers:
            create_kwargs["extra_headers"] = extra_headers

        response = await asyncio.to_thread(
            self.client.messages.create,
            **create_kwargs,
        )

        # Convert content blocks to serializable dicts. Unknown block types
        # (server_tool_use, web_search_tool_result, etc.) are preserved
        # verbatim — Anthropic rejects subsequent requests if the prior
        # assistant turn is missing them.
        content = []
        for block in response.content:
            if block.type == "text":
                content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
            else:
                content.append(_block_to_dict(block))

        return {
            "content": content,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
                "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
            },
            "model": model,
            "stop_reason": response.stop_reason,
        }

    async def stream_completion_with_tools(
        self,
        system_prompt: Any,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        model_tier: str = "balanced",
        max_tokens: int = 16384,
        context_management: dict | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream completion with tool-use via Claude API."""
        model = self.get_model(model_tier)

        # Translate builtin markers into Anthropic's tool format; drop others.
        tools = self._convert_tools(tools)
        extra_headers = self._beta_headers_for(tools)

        if isinstance(system_prompt, str):
            if self.settings.PROMPT_CACHING_ENABLED:
                system = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
            else:
                system = system_prompt
        else:
            system = system_prompt

        logger.debug("anthropic_stream_outbound: %s", _summarize_messages(messages))

        stream_kwargs: Dict[str, Any] = dict(
            model=model, max_tokens=max_tokens,
            system=system, messages=messages, tools=tools,
        )
        if context_management:
            extra_headers = extra_headers or {}
            extra_headers["anthropic-beta"] = (
                extra_headers["anthropic-beta"] + ",context-management-2025-06-27"
                if "anthropic-beta" in extra_headers
                else "context-management-2025-06-27"
            )
            stream_kwargs["extra_body"] = {"context_management": context_management}
        if extra_headers:
            stream_kwargs["extra_headers"] = extra_headers

        # Run sync streaming in a thread, bridge to async via queue.
        # Event items are ("event", sdk_event); the fully-assembled Message
        # arrives as ("final", Message) after the stream closes; any
        # exception becomes ("error", e).
        queue: asyncio.Queue = asyncio.Queue()
        _sentinel = object()

        def _run_sync_stream():
            try:
                with self.client.messages.stream(**stream_kwargs) as stream:
                    for event in stream:
                        queue.put_nowait(("event", event))
                    try:
                        queue.put_nowait(("final", stream.get_final_message()))
                    except Exception as e:
                        logger.warning("Anthropic get_final_message failed: %s", e)
            except Exception as e:
                queue.put_nowait(("error", e))
            queue.put_nowait(_sentinel)

        asyncio.get_event_loop().run_in_executor(None, _run_sync_stream)

        # Streaming path only drives UI-visible events (text, tool_use rows,
        # web_search rows). The ("final", Message) item carries the full
        # assembled content via ``message_complete`` — that's what the
        # consumer persists to history. Keep per-index state only for
        # what the UI events need.
        block_types_by_index: Dict[int, str] = {}
        tool_ids_by_index: Dict[int, str] = {}
        server_tool_by_index: Dict[int, Dict[str, Any]] = {}
        web_result_by_index: Dict[int, Any] = {}
        final_usage: Dict[str, Any] = {}
        final_stop_reason = "end_turn"

        while True:
            item = await queue.get()
            if item is _sentinel:
                break
            kind, payload = item

            if kind == "error":
                raise payload

            if kind == "final":
                if payload is None:
                    continue
                if hasattr(payload, "content"):
                    yield StreamChunk(
                        type="message_complete",
                        blocks=[_block_to_dict(b) for b in payload.content],
                    )
                usage = getattr(payload, "usage", None)
                if usage is not None:
                    final_usage = {
                        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
                        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
                    }
                final_stop_reason = getattr(payload, "stop_reason", None) or final_stop_reason
                continue

            event = payload
            etype = event.type

            if etype == "content_block_start":
                idx = getattr(event, "index", -1)
                block = getattr(event, "content_block", None)
                btype = getattr(block, "type", "") if block is not None else ""
                block_types_by_index[idx] = btype
                if btype == "tool_use":
                    tool_ids_by_index[idx] = block.id
                    yield StreamChunk(
                        type="tool_use_start",
                        tool_name=block.name,
                        tool_id=block.id,
                    )
                elif btype == "server_tool_use":
                    server_tool_by_index[idx] = {
                        "id": getattr(block, "id", "") or "",
                        "name": getattr(block, "name", "") or "",
                        "input_json": "",
                    }
                elif btype in ("web_search_tool_result", "web_fetch_tool_result"):
                    # Delivered whole in content_block_start; surfaced to
                    # the UI at content_block_stop via builtin_tool_result.
                    web_result_by_index[idx] = block

            elif etype == "content_block_delta":
                idx = getattr(event, "index", -1)
                delta = event.delta
                dtype = getattr(delta, "type", "")
                if dtype == "text_delta":
                    yield StreamChunk(type="text_delta", text=delta.text)
                elif dtype == "input_json_delta":
                    btype = block_types_by_index.get(idx, "")
                    if btype == "tool_use":
                        yield StreamChunk(type="tool_input_delta", tool_input_json=delta.partial_json)
                    elif btype == "server_tool_use":
                        server_tool_by_index[idx]["input_json"] += delta.partial_json

            elif etype == "content_block_stop":
                idx = getattr(event, "index", -1)
                btype = block_types_by_index.pop(idx, "")
                if btype == "tool_use":
                    yield StreamChunk(type="tool_use_end", tool_id=tool_ids_by_index.pop(idx, ""))
                elif btype == "server_tool_use":
                    info = server_tool_by_index.pop(idx, {})
                    query = _parse_server_tool_query(info.get("input_json", ""))
                    yield StreamChunk(
                        type="builtin_tool_use",
                        tool_name=info.get("name", "web_search"),
                        tool_id=info.get("id", ""),
                        text=query,
                        stop_reason="completed",
                    )
                elif btype in ("web_search_tool_result", "web_fetch_tool_result"):
                    result_block = web_result_by_index.pop(idx, None)
                    if result_block is not None:
                        if btype == "web_search_tool_result":
                            hits, error_code = _parse_web_search_hits(result_block)
                        else:
                            hits, error_code = _parse_web_fetch_result(result_block)
                        tool_use_id = str(_get_field(result_block, "tool_use_id", "") or "")
                        # Diagnostic: helps trace whether the stream actually
                        # carries hits at content_block_stop, or only in the
                        # final-assembled message (get_final_message path).
                        logger.info(
                            "anthropic_result_stream: type=%s tool_use_id=%s hits=%d error=%s block_content_present=%s",
                            btype, tool_use_id, len(hits), error_code,
                            _get_field(result_block, "content") is not None,
                        )
                        yield StreamChunk(
                            type="builtin_tool_result",
                            tool_name=(
                                "web_search" if btype == "web_search_tool_result" else "web_fetch"
                            ),
                            tool_id=tool_use_id,
                            hits=hits,
                            text=error_code,
                        )

            elif etype == "message_delta":
                final_stop_reason = getattr(event.delta, "stop_reason", final_stop_reason)
                usage = getattr(event, "usage", None)
                if usage is not None:
                    for key in ("input_tokens", "output_tokens",
                                "cache_creation_input_tokens", "cache_read_input_tokens"):
                        val = getattr(usage, key, 0) or 0
                        if val:
                            final_usage[key] = val

        yield StreamChunk(type="done", stop_reason=final_stop_reason, usage=final_usage)

    def supports_vision(self) -> bool:
        return True

    def supports_prompt_caching(self) -> bool:
        return True

    def format_image_content(self, base64_image: str, media_type: str = "image/png") -> Dict[str, Any]:
        """Format image for Anthropic's message format"""
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64_image
            }
        }


class OpenAIProvider(LLMProvider):
    """OpenAI GPT provider — uses the Responses API.

    Supports built-in tools: web_search_preview, code_interpreter.
    Custom function tools work alongside built-in tools.
    """

    def __init__(self):
        from openai import OpenAI
        from app.config import settings

        self.client = OpenAI(api_key=settings.OPENAI_API_KEY)
        self.settings = settings
        self._models = {
            "fast": settings.OPENAI_MODEL_FAST,
            "balanced": settings.OPENAI_MODEL_BALANCED,
        }

    @property
    def name(self) -> str:
        return "OpenAI"

    def get_model(self, tier: str) -> str:
        return self._models.get(tier, tier)
    
    async def create_completion(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        model_tier: str = "balanced",
        max_tokens: int = 8192,
        use_cache: bool = True  # Ignored - OpenAI doesn't support prompt caching
    ) -> Dict[str, Any]:
        """Create completion using OpenAI API"""
        model = self.get_model(model_tier)
        
        # Build messages with system prompt
        full_messages = [{"role": "system", "content": system_prompt}]
        
        # Convert Anthropic-style messages to OpenAI format
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content")
            
            if isinstance(content, str):
                full_messages.append({"role": role, "content": content})
            elif isinstance(content, list):
                # Handle multimodal content (images + text)
                openai_content = []
                for item in content:
                    if item.get("type") == "text":
                        openai_content.append({
                            "type": "text",
                            "text": item.get("text", "")
                        })
                    elif item.get("type") == "image":
                        # Convert Anthropic image format to OpenAI
                        source = item.get("source", {})
                        if source.get("type") == "base64":
                            openai_content.append({
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}"
                                }
                            })
                full_messages.append({"role": role, "content": openai_content})
            else:
                full_messages.append({"role": role, "content": str(content)})
        
        # Run synchronous API call in thread pool
        response = await asyncio.to_thread(
            self.client.chat.completions.create,
            model=model,
            max_tokens=max_tokens,
            messages=full_messages
        )
        
        cached = 0
        try:
            details = getattr(response.usage, "prompt_tokens_details", None)
            if details is not None:
                cached = getattr(details, "cached_tokens", 0) or 0
        except Exception:
            cached = 0

        return {
            "content": response.choices[0].message.content,
            "usage": {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": cached,
            },
            "model": model,
            "stop_reason": response.choices[0].finish_reason
        }
    
    async def create_completion_with_tools(
        self,
        system_prompt: Any,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        model_tier: str = "balanced",
        max_tokens: int = 16384,
    ) -> Dict[str, Any]:
        """Create completion with tool-use via OpenAI function-calling API.

    def _extract_instructions(self, system_prompt: Any) -> str:
        """Extract plain text from system prompt (string or Anthropic content blocks)."""
        if isinstance(system_prompt, list):
            return " ".join(
                block.get("text", "") for block in system_prompt if block.get("type") == "text"
            )
        return system_prompt or ""

    def _convert_tools(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert Anthropic tool format to Responses API flat format.

        Function tools are converted as usual. Built-in tools marked for
        OpenAI (``provider == "openai"``, or legacy markers with no provider)
        have their spec forwarded. Builtins for other providers are dropped.
        """
        openai_tools = []
        for tool in tools:
            if tool.get("__builtin__"):
                provider = tool.get("provider", "")
                if provider and provider != "openai":
                    continue
                spec = dict(tool.get("spec") or {})
                spec.pop("provider", None)
                if spec:
                    openai_tools.append(spec)
                continue
            openai_tools.append({
                "type": "function",
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {}),
            })
        return openai_tools

    def _convert_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert Anthropic-format messages to Responses API input items."""
        import json as json_lib
        input_items: List[Dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content")

            if role == "user" and isinstance(content, str):
                input_items.append({"role": "user", "content": content})

            elif role == "user" and isinstance(content, list):
                # Split into tool_results (separate "tool" role messages) and
                # mixed user content (text + images → single user message).
                tool_results: list[dict] = []
                user_parts: list[dict] = []
                for item in content:
                    itype = item.get("type")
                    if itype == "tool_result":
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": item.get("tool_use_id", ""),
                            "content": item.get("content", ""),
                        })
                    elif itype == "text":
                        user_parts.append({"type": "text", "text": item["text"]})
                    elif itype == "image_url":
                        user_parts.append(item)  # OpenAI native format, passthrough
                    elif itype == "image":
                        # Anthropic format → convert to OpenAI image_url
                        src = item.get("source", {})
                        b64 = src.get("data", "")
                        mt = src.get("media_type", "image/png")
                        if b64:
                            user_parts.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{mt};base64,{b64}"},
                            })
                            logger.info(
                                "OpenAI: converted Anthropic image block to "
                                "image_url (mime=%s, b64_len=%d)", mt, len(b64),
                            )
                    else:
                        logger.warning(
                            "OpenAI: dropping unknown user content item type=%s",
                            itype,
                        )

                if user_parts:
                    has_image = any(p.get("type") == "image_url" for p in user_parts)
                    if has_image:
                        logger.info(
                            "OpenAI: sending user message with %d image_url + "
                            "%d text part(s) to model=%s",
                            sum(1 for p in user_parts if p["type"] == "image_url"),
                            sum(1 for p in user_parts if p["type"] == "text"),
                            model,
                        )
                        full_messages.append({"role": "user", "content": user_parts})
                    else:
                        merged = "\n".join(
                            p["text"] for p in user_parts if p.get("type") == "text"
                        )
                        full_messages.append({"role": "user", "content": merged})
                full_messages.extend(tool_results)
            else:
                full_messages.append({"role": role, "content": str(content) if content else ""})

            elif role == "assistant" and isinstance(content, list):
                for item in content:
                    if item.get("type") == "text":
                        input_items.append({"role": "assistant", "content": item["text"]})
                    elif item.get("type") == "tool_use":
                        input_items.append({
                            "type": "function_call",
                            "call_id": item["id"],
                            "name": item["name"],
                            "arguments": json_lib.dumps(item["input"]),
                        })

            elif role == "assistant" and isinstance(content, str):
                input_items.append({"role": "assistant", "content": content})

            else:
                input_items.append({"role": role, "content": str(content) if content else ""})

        return input_items

    def _convert_response(self, response) -> Dict[str, Any]:
        """Convert Responses API response to Anthropic-style content blocks."""
        import json as json_lib
        content_blocks: List[Dict[str, Any]] = []
        if choice.message.content:
            content_blocks.append({"type": "text", "text": choice.message.content})
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                args = _safe_parse_tool_args(tc.function.arguments, tc.function.name, model)
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": args,
                })
            # web_search_call items are auto-executed server-side, skip

        has_function_calls = any(item.type == "function_call" for item in response.output)
        stop_reason = "tool_use" if has_function_calls else "end_turn"

        # OpenAI gpt-4o and later support automatic prompt caching. The cached
        # token count is reported in usage.prompt_tokens_details.cached_tokens.
        cached = 0
        try:
            details = getattr(response.usage, "prompt_tokens_details", None)
            if details is not None:
                cached = getattr(details, "cached_tokens", 0) or 0
        except Exception:
            cached = 0

        return {
            "content": content_blocks,
            "usage": {
                "input_tokens": getattr(response.usage, 'input_tokens', 0),
                "output_tokens": getattr(response.usage, 'output_tokens', 0),
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": cached,
            },
            "model": response.model,
            "stop_reason": stop_reason,
        }

    async def create_completion(
        self, system_prompt: str, messages: List[Dict[str, Any]],
        model_tier: str = "balanced", max_tokens: int = 8192, use_cache: bool = True,
    ) -> Dict[str, Any]:
        """Create completion using Responses API."""
        model = self.get_model(model_tier)
        input_items = self._convert_messages(messages)

        response = await asyncio.to_thread(
            self.client.responses.create,
            model=model,
            instructions=system_prompt,
            input=input_items,
            max_output_tokens=max_tokens,
            store=False,
        )

        return {
            "content": response.output_text,
            "usage": {
                "input_tokens": getattr(response.usage, 'input_tokens', 0),
                "output_tokens": getattr(response.usage, 'output_tokens', 0),
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            "model": model,
            "stop_reason": "end_turn",
        }

    async def create_completion_with_tools(
        self, system_prompt: Any, messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]], model_tier: str = "balanced",
        max_tokens: int = 16384,
    ) -> Dict[str, Any]:
        """Create completion with tool-use via Responses API."""
        model = self.get_model(model_tier)
        instructions = self._extract_instructions(system_prompt)
        input_items = self._convert_messages(messages)
        openai_tools = self._convert_tools(tools)

        is_reasoning_model = model.startswith("o")
        kwargs: Dict[str, Any] = {
            "model": model,
            "instructions": instructions,
            "input": input_items,
            "tools": openai_tools if openai_tools else None,
            "max_output_tokens": max_tokens,
            "store": False,
        }
        if is_reasoning_model:
            kwargs["reasoning"] = {"effort": "medium"}

        response = await asyncio.to_thread(
            self.client.responses.create, **kwargs
        )

        return self._convert_response(response)

    async def stream_completion_with_tools(
        self, system_prompt: Any, messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]], model_tier: str = "balanced",
        max_tokens: int = 16384,
        context_management: dict | None = None,
        extra_request_kwargs: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream completion with tool-use via Responses API.

        ``extra_request_kwargs`` is merged into the ``responses.create`` call —
        used by callers to inject provider-specific knobs such as
        ``tool_choice={"type": "web_search_preview"}`` for a single turn.
        """
        model = self.get_model(model_tier)
        instructions = self._extract_instructions(system_prompt)
        input_items = self._convert_messages(messages)
        openai_tools = self._convert_tools(tools)

        queue: asyncio.Queue = asyncio.Queue()
        _sentinel = object()

        # Enable reasoning summary for o-series models
        is_reasoning_model = model.startswith("o")
        reasoning_config = {"effort": "medium"} if is_reasoning_model else None

        def _run_stream():
            kwargs: Dict[str, Any] = {
                "model": model,
                "instructions": instructions,
                "input": input_items,
                "tools": openai_tools if openai_tools else None,
                "max_output_tokens": max_tokens,
                "stream": True,
                "store": False,
            }
            if reasoning_config:
                kwargs["reasoning"] = reasoning_config
            if extra_request_kwargs:
                kwargs.update(extra_request_kwargs)
            stream = self.client.responses.create(**kwargs)
            for event in stream:
                queue.put_nowait(event)
            queue.put_nowait(_sentinel)

        asyncio.get_event_loop().run_in_executor(None, _run_stream)

        current_func_name = ""
        current_func_id = ""
        func_args_buffer = ""
        has_tool_calls = False
        final_usage: Dict[str, Any] = {}

        while True:
            event = await queue.get()
            if event is _sentinel:
                break

            etype = getattr(event, 'type', '')

            if etype == "response.output_text.delta":
                yield StreamChunk(type="text_delta", text=event.delta)

            elif etype == "response.reasoning_summary_text.delta":
                yield StreamChunk(type="reasoning_delta", text=event.delta)

            elif etype == "response.output_item.added":
                item = getattr(event, 'item', None)
                item_type = getattr(item, 'type', '') if item is not None else ''
                if item_type == "function_call":
                    current_func_name = item.name
                    current_func_id = item.call_id
                    func_args_buffer = ""
                    has_tool_calls = True
                    yield StreamChunk(type="tool_use_start",
                        tool_name=current_func_name, tool_id=current_func_id)
                elif item_type == "web_search_call":
                    queries = _extract_web_search_queries(item)
                    logger.info("web_search_event: type=output_item.added id=%s queries=%r item_attrs=%s",
                                getattr(item, 'id', ''), queries,
                                _dump_attrs(item))
                    for q in queries:
                        yield StreamChunk(
                            type="builtin_tool_use",
                            tool_name="web_search",
                            tool_id=getattr(item, 'id', '') or '',
                            text=q,
                        )

            elif etype == "response.output_item.done":
                # web_search_call queries are typically populated by the
                # time the item completes — emit one chunk per query.
                item = getattr(event, 'item', None)
                if item is not None and getattr(item, 'type', '') == "web_search_call":
                    queries = _extract_web_search_queries(item)
                    logger.info("web_search_event: type=output_item.done id=%s queries=%r",
                                getattr(item, 'id', ''), queries)
                    for q in queries:
                        yield StreamChunk(
                            type="builtin_tool_use",
                            tool_name="web_search",
                            tool_id=getattr(item, 'id', '') or '',
                            text=q,
                            stop_reason="completed",
                        )

            elif etype == "response.function_call_arguments.delta":
                func_args_buffer += event.delta

            elif etype == "response.function_call_arguments.done":
                yield StreamChunk(type="tool_input_delta",
                    tool_id=current_func_id, tool_input_json=func_args_buffer)
                yield StreamChunk(type="tool_use_end", tool_id=current_func_id)
                func_args_buffer = ""

            elif etype in ("response.web_search_call.searching",
                           "response.web_search_call.in_progress",
                           "response.web_search_call.completed"):
                query = _extract_web_search_query(event)
                status = etype.rsplit('.', 1)[-1]
                logger.info("web_search_event: type=%s query=%r event_attrs=%s",
                            etype, query, _dump_attrs(event))
                yield StreamChunk(
                    type="builtin_tool_use",
                    tool_name="web_search",
                    tool_id=getattr(event, 'item_id', '') or '',
                    text=query or '',
                    stop_reason=status,
                )

            elif etype == "response.completed":
                if hasattr(event, 'response') and hasattr(event.response, 'usage'):
                    u = event.response.usage
                    final_usage = {
                        "input_tokens": getattr(u, 'input_tokens', 0),
                        "output_tokens": getattr(u, 'output_tokens', 0),
                    }

        yield StreamChunk(type="done",
            stop_reason="tool_use" if has_tool_calls else "end_turn",
            usage=final_usage)

    def supports_vision(self) -> bool:
        return True

    def supports_prompt_caching(self) -> bool:
        # gpt-4o and later models support automatic prompt caching (no cache_control
        # needed; OpenAI caches 1024+ token prefixes automatically)
        return True

    def format_image_content(self, base64_image: str, media_type: str = "image/png") -> Dict[str, Any]:
        return {
            "type": "image_url",
            "image_url": {
                "url": f"data:{media_type};base64,{base64_image}"
            }
        }


class DeepSeekProvider(LLMProvider):
    """DeepSeek provider — OpenAI-compatible Chat Completions API.

    Uses Chat Completions API (not Responses API) since DeepSeek
    doesn't support OpenAI's Responses API.

    Supports V3.2 thinking mode with tool use via
    ``extra_body={"thinking": {"type": "enabled"}}``.
    """

    _THINKING_MIN_MAX_TOKENS = 16384

    def __init__(self):
        from openai import OpenAI
        from app.config import settings

        self.client = OpenAI(
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_BASE_URL,
        )
        self.settings = settings
        self._models = {
            "fast": settings.DEEPSEEK_MODEL_FAST,
            "balanced": settings.DEEPSEEK_MODEL_BALANCED,
        }

    @property
    def name(self) -> str:
        return "DeepSeek"

    def get_model(self, tier: str) -> str:
        return self._models.get(tier, tier)

    def _is_thinking_tier(self, model_tier: str) -> bool:
        if not self.settings.DEEPSEEK_THINKING_ENABLED:
            return False
        return model_tier in ("balanced", self._models.get("balanced", ""))

    async def create_completion(
        self, system_prompt: str, messages: List[Dict[str, Any]],
        model_tier: str = "balanced", max_tokens: int = 8192, use_cache: bool = True,
    ) -> Dict[str, Any]:
        """Create completion using Chat Completions API."""
        model = self.get_model(model_tier)
        full_messages = [{"role": "system", "content": system_prompt}]
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content")
            if isinstance(content, str):
                full_messages.append({"role": role, "content": content})
            else:
                full_messages.append({"role": role, "content": str(content) if content else ""})

        response = await asyncio.to_thread(
            self.client.chat.completions.create,
            model=model, max_tokens=max_tokens, messages=full_messages,
        )
        return {
            "content": response.choices[0].message.content,
            "usage": {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            "model": model,
            "stop_reason": response.choices[0].finish_reason,
        }

    def supports_vision(self) -> bool:
        return True

    def supports_prompt_caching(self) -> bool:
        return False

    async def create_completion_with_tools(
        self,
        system_prompt: Any,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        model_tier: str = "balanced",
        max_tokens: int = 16384,
    ) -> Dict[str, Any]:
        """Create completion with tool-use, optionally with thinking mode.

        When thinking is enabled, adds ``reasoning_content`` passthrough
        for multi-turn tool-use loops as required by the DeepSeek API.
        """
        import json as json_lib

        model = self.get_model(model_tier)
        thinking = self._is_thinking_tier(model_tier)

        # --- Convert system prompt ---
        if isinstance(system_prompt, list):
            sys_text = " ".join(
                block.get("text", "") for block in system_prompt if block.get("type") == "text"
            )
        else:
            sys_text = system_prompt

        full_messages: List[Dict[str, Any]] = [{"role": "system", "content": sys_text}]

        # --- Convert Anthropic-style messages to OpenAI format ---
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content")

            if role == "assistant" and isinstance(content, list):
                text_parts = []
                tool_calls = []
                for item in content:
                    if item.get("type") == "text":
                        text_parts.append(item["text"])
                    elif item.get("type") == "tool_use":
                        tool_calls.append({
                            "id": item["id"],
                            "type": "function",
                            "function": {
                                "name": item["name"],
                                "arguments": json_lib.dumps(item["input"]),
                            },
                        })
                oai_msg: Dict[str, Any] = {"role": "assistant"}
                if text_parts:
                    oai_msg["content"] = "\n".join(text_parts)
                if tool_calls:
                    oai_msg["tool_calls"] = tool_calls
                # Pass reasoning_content back for thinking mode continuity
                reasoning = msg.get("_reasoning_content")
                if reasoning and thinking:
                    oai_msg["reasoning_content"] = reasoning
                full_messages.append(oai_msg)

            elif role == "user" and isinstance(content, list):
                # Split into tool_results (separate "tool" role messages) vs
                # mixed text+image content (a single user message).
                tool_results: list[dict] = []
                user_parts: list[dict] = []
                for item in content:
                    itype = item.get("type")
                    if itype == "tool_result":
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": item.get("tool_use_id", ""),
                            "content": item.get("content", ""),
                        })
                    elif itype == "text":
                        user_parts.append({"type": "text", "text": item["text"]})
                    elif itype == "image_url":
                        # OpenAI/DeepSeek native format — pass through unchanged
                        user_parts.append(item)
                    elif itype == "image":
                        # Anthropic format → convert to OpenAI image_url shape
                        src = item.get("source", {})
                        b64 = src.get("data", "")
                        mt = src.get("media_type", "image/png")
                        if b64:
                            user_parts.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{mt};base64,{b64}"},
                            })
                            logger.info(
                                "DeepSeek: converted Anthropic image block to "
                                "image_url (mime=%s, b64_len=%d)", mt, len(b64),
                            )
                    else:
                        logger.warning(
                            "DeepSeek: dropping unknown user content item type=%s",
                            itype,
                        )

                if user_parts:
                    # If only text, collapse to a string for older DeepSeek
                    # compatibility; mixed (text+image) needs the list form.
                    has_image = any(p.get("type") == "image_url" for p in user_parts)
                    if has_image:
                        logger.info(
                            "DeepSeek: sending user message with %d image_url part(s) "
                            "and %d text part(s) to model=%s",
                            sum(1 for p in user_parts if p["type"] == "image_url"),
                            sum(1 for p in user_parts if p["type"] == "text"),
                            model,
                        )
                        full_messages.append({"role": "user", "content": user_parts})
                    else:
                        merged = "\n".join(
                            p["text"] for p in user_parts if p.get("type") == "text"
                        )
                        full_messages.append({"role": "user", "content": merged})
                full_messages.extend(tool_results)
            else:
                full_messages.append({"role": role, "content": str(content) if content else ""})

        # --- Convert tools ---
        # Built-in tool markers (e.g. OpenAI's web_search) are OpenAI-Responses-only;
        # drop them for chat.completions providers.
        openai_tools = []
        for tool in tools:
            if tool.get("__builtin__"):
                continue
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            })

        # --- Build API kwargs ---
        effective_max_tokens = max(max_tokens, self._THINKING_MIN_MAX_TOKENS) if thinking else max_tokens
        kwargs: Dict[str, Any] = {
            "model": model,
            "max_tokens": effective_max_tokens,
            "messages": full_messages,
            "tools": openai_tools if openai_tools else None,
        }
        if thinking:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

        response = await asyncio.to_thread(
            self.client.chat.completions.create, **kwargs
        )

        choice = response.choices[0]

        # --- Convert response to Anthropic-style content blocks ---
        content_blocks: List[Dict[str, Any]] = []
        if choice.message.content:
            content_blocks.append({"type": "text", "text": choice.message.content})
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                args = _safe_parse_tool_args(tc.function.arguments, tc.function.name, model)
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": args,
                })

        stop_reason = "end_turn"
        if choice.finish_reason == "tool_calls":
            stop_reason = "tool_use"

        result: Dict[str, Any] = {
            "content": content_blocks,
            "usage": {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            "model": model,
            "stop_reason": stop_reason,
        }

        # Extract reasoning_content for thinking mode
        reasoning_content = getattr(choice.message, "reasoning_content", None)
        if reasoning_content:
            result["reasoning_content"] = reasoning_content

        return result

    def supports_vision(self) -> bool:
        # Verified empirically 2026-04: DeepSeek's hosted API returns
        #   400 Bad Request: "unknown variant image_url, expected text"
        # for both `deepseek-chat` and `deepseek-reasoner`. Their vision models
        # (DeepSeek-VL2, Janus-Pro) are only distributed as HuggingFace weights,
        # not served on api.deepseek.com. If you self-host one and expose it via
        # an OpenAI-compatible endpoint, point DEEPSEEK_BASE_URL at it and then
        # override this method in a subclass.
        return False

        if isinstance(system_prompt, list):
            sys_text = " ".join(
                block.get("text", "") for block in system_prompt if block.get("type") == "text"
            )
        else:
            sys_text = system_prompt

        full_messages: List[Dict[str, Any]] = [{"role": "system", "content": sys_text}]
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content")
            if role == "assistant" and isinstance(content, list):
                text_parts = []
                tool_calls = []
                for item in content:
                    if item.get("type") == "text":
                        text_parts.append(item["text"])
                    elif item.get("type") == "tool_use":
                        tool_calls.append({
                            "id": item["id"], "type": "function",
                            "function": {"name": item["name"], "arguments": json_lib.dumps(item["input"])},
                        })
                oai_msg: Dict[str, Any] = {"role": "assistant"}
                if text_parts:
                    oai_msg["content"] = "\n".join(text_parts)
                if tool_calls:
                    oai_msg["tool_calls"] = tool_calls
                full_messages.append(oai_msg)
            elif role == "user" and isinstance(content, list):
                for item in content:
                    if item.get("type") == "tool_result":
                        full_messages.append({
                            "role": "tool",
                            "tool_call_id": item.get("tool_use_id", ""),
                            "content": item.get("content", ""),
                        })
                    elif item.get("type") == "text":
                        full_messages.append({"role": "user", "content": item["text"]})
            else:
                full_messages.append({"role": role, "content": str(content) if content else ""})

        openai_tools = [
            {"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("input_schema", {})}}
            for t in tools
            if not t.get("__builtin__")
        ]

        queue: asyncio.Queue = asyncio.Queue()
        _sentinel = object()

        def _run_sync_stream():
            stream = self.client.chat.completions.create(
                model=model, max_tokens=max_tokens,
                messages=full_messages,
                tools=openai_tools if openai_tools else None,
                stream=True,
                stream_options={"include_usage": True},
            )
            for c in stream:
                queue.put_nowait(c)
            queue.put_nowait(_sentinel)

        asyncio.get_event_loop().run_in_executor(None, _run_sync_stream)

        tool_call_buffer: Dict[int, Dict[str, str]] = {}
        final_stop_reason = "end_turn"
        final_usage: Dict[str, Any] = {}

        while True:
            chunk = await queue.get()
            if chunk is _sentinel:
                break
            if hasattr(chunk, 'usage') and chunk.usage:
                final_usage = {
                    "input_tokens": chunk.usage.prompt_tokens or 0,
                    "output_tokens": chunk.usage.completion_tokens or 0,
                }
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            finish_reason = chunk.choices[0].finish_reason
            if delta and delta.content:
                yield StreamChunk(type="text_delta", text=delta.content)
            if delta and delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_call_buffer:
                        tool_call_buffer[idx] = {"id": tc.id or "", "name": "", "arguments": ""}
                    if tc.function and tc.function.name:
                        tool_call_buffer[idx]["name"] = tc.function.name
                        yield StreamChunk(type="tool_use_start",
                            tool_name=tc.function.name, tool_id=tc.id or tool_call_buffer[idx]["id"])
                    if tc.function and tc.function.arguments:
                        tool_call_buffer[idx]["arguments"] += tc.function.arguments
            if finish_reason:
                final_stop_reason = "tool_use" if finish_reason == "tool_calls" else "end_turn"
                for idx, tc_data in tool_call_buffer.items():
                    if tc_data["arguments"]:
                        yield StreamChunk(type="tool_input_delta",
                            tool_id=tc_data["id"], tool_input_json=tc_data["arguments"])
                    yield StreamChunk(type="tool_use_end", tool_id=tc_data["id"])

        yield StreamChunk(type="done", stop_reason=final_stop_reason, usage=final_usage)


# Per-provider cache: multiple providers can coexist (e.g. Anthropic for AppBuilder,
# OpenAI for a future Ad Builder agent).
_providers: dict[str, LLMProvider] = {}


def get_llm_provider(provider_name: str | None = None) -> LLMProvider:
    """
    Get an LLM provider by name, with per-provider caching.

    Args:
        provider_name: "anthropic" or "openai".  If None, falls back to
                       the global ``settings.LLM_PROVIDER`` default.

    Returns:
        Cached LLMProvider instance.
    """
    from app.config import settings

    name = (provider_name or settings.LLM_PROVIDER).lower()

    if name in _providers:
        return _providers[name]

    if name == "deepseek":
        if not settings.DEEPSEEK_API_KEY:
            raise ValueError("DEEPSEEK_API_KEY is required when using the deepseek provider")
        _providers[name] = DeepSeekProvider()
        logger.info(f"Initialized DeepSeek provider with models: {settings.DEEPSEEK_MODEL_FAST}, {settings.DEEPSEEK_MODEL_BALANCED}")
    elif name == "openai":
        if not settings.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY is required when using the openai provider")
        _providers[name] = OpenAIProvider()
        logger.info(f"Initialized OpenAI provider with models: {settings.OPENAI_MODEL_FAST}, {settings.OPENAI_MODEL_BALANCED}")
    else:
        if not settings.ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY is required when using the anthropic provider")
        _providers[name] = AnthropicProvider()
        logger.info(f"Initialized Anthropic provider with models: {settings.CLAUDE_HAIKU}, {settings.CLAUDE_SONNET}")

    return _providers[name]


def get_available_models() -> list[dict[str, str]]:
    """Return all available models across configured providers.

    Only includes providers that have API keys set.

    Returns:
        List of dicts with ``id`` (provider_name:model_name) and
        ``name`` (display name in "Provider - Model" format).
    """
    from app.config import settings

    # (provider_key, display_name, tier, model_name, label_suffix)
    model_entries: list[tuple[str, str, str, str, str]] = [
        ("anthropic", "Anthropic", "fast", settings.CLAUDE_HAIKU, ""),
        ("anthropic", "Anthropic", "balanced", settings.CLAUDE_SONNET, ""),
        ("openai", "OpenAI", "fast", settings.OPENAI_MODEL_FAST, ""),
        ("openai", "OpenAI", "balanced", settings.OPENAI_MODEL_BALANCED, ""),
        ("deepseek", "DeepSeek", "fast", settings.DEEPSEEK_MODEL_FAST, ""),
        ("deepseek", "DeepSeek", "balanced", settings.DEEPSEEK_MODEL_BALANCED,
         " (thinking)" if settings.DEEPSEEK_THINKING_ENABLED else ""),
    ]

    api_keys = {
        "anthropic": settings.ANTHROPIC_API_KEY,
        "openai": settings.OPENAI_API_KEY,
        "deepseek": settings.DEEPSEEK_API_KEY,
    }

    models: list[dict[str, str]] = []
    seen: set[str] = set()

    for provider_key, display_name, _tier, model_name, suffix in model_entries:
        if not api_keys.get(provider_key):
            continue
        composite_id = f"{provider_key}:{model_name}"
        if composite_id in seen:
            continue
        seen.add(composite_id)
        models.append({
            "id": composite_id,
            "name": f"{display_name} - {model_name}{suffix}",
        })

    return models


def resolve_model_override(model_id: str) -> tuple[str, str]:
    """Parse a model override ID into (provider_name, model_name).

    Args:
        model_id: Composite ID in "provider:model" format.

    Returns:
        (provider_name, model_name) tuple.

    Raises:
        ValueError: If the format is invalid.
    """
    if ":" not in model_id:
        raise ValueError(f"Invalid model ID format: {model_id}. Expected 'provider:model'.")
    provider, model = model_id.split(":", 1)
    return provider, model


def reset_provider():
    """Reset all cached providers (useful for testing)."""
    global _providers
    _providers = {}

