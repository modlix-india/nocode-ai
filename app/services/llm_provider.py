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
from typing import List, Dict, Any, Optional
import logging
import asyncio

logger = logging.getLogger(__name__)


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

        response = await asyncio.to_thread(
            self.client.messages.create,
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=tools,
        )

        # Convert content blocks to serializable dicts
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
    """OpenAI GPT provider"""

    def __init__(self):
        from openai import OpenAI
        from app.config import settings

        self.client = OpenAI(api_key=settings.OPENAI_API_KEY)
        self.settings = settings
        self._models = {
            "fast": settings.OPENAI_MODEL_FAST,
            "balanced": settings.OPENAI_MODEL_BALANCED
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
        
        return {
            "content": response.choices[0].message.content,
            "usage": {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0
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

        Maps Anthropic tool format to OpenAI function-calling format,
        and maps response back to Anthropic-style content blocks.
        """
        import json as json_lib
        model = self.get_model(model_tier)

        # Extract system prompt text
        if isinstance(system_prompt, list):
            sys_text = " ".join(
                block.get("text", "") for block in system_prompt if block.get("type") == "text"
            )
        else:
            sys_text = system_prompt

        full_messages = [{"role": "system", "content": sys_text}]

        # Convert Anthropic-style messages to OpenAI format
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content")

            if role == "assistant" and isinstance(content, list):
                # Handle assistant messages with tool_use blocks
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
                full_messages.append(oai_msg)

            elif role == "user" and isinstance(content, list):
                # Handle tool_result blocks → OpenAI tool messages
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

        # Convert Anthropic tools to OpenAI function-calling format
        openai_tools = []
        for tool in tools:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            })

        response = await asyncio.to_thread(
            self.client.chat.completions.create,
            model=model,
            max_tokens=max_tokens,
            messages=full_messages,
            tools=openai_tools if openai_tools else None,
        )

        choice = response.choices[0]

        # Convert OpenAI response to Anthropic-style content blocks
        content_blocks: List[Dict[str, Any]] = []
        if choice.message.content:
            content_blocks.append({"type": "text", "text": choice.message.content})
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": json_lib.loads(tc.function.arguments),
                })

        # Map OpenAI finish_reason to Anthropic stop_reason
        stop_reason = "end_turn"
        if choice.finish_reason == "tool_calls":
            stop_reason = "tool_use"

        return {
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

    def supports_vision(self) -> bool:
        return True

    def supports_prompt_caching(self) -> bool:
        return False

    def format_image_content(self, base64_image: str, media_type: str = "image/png") -> Dict[str, Any]:
        """Format image for OpenAI's message format"""
        return {
            "type": "image_url",
            "image_url": {
                "url": f"data:{media_type};base64,{base64_image}"
            }
        }


class DeepSeekProvider(OpenAIProvider):
    """DeepSeek provider — OpenAI-compatible API at api.deepseek.com.

    Supports V3.2 thinking mode with tool use via
    ``extra_body={"thinking": {"type": "enabled"}}``.

    When thinking is enabled:
    - ``reasoning_content`` from each response must be passed back in
      subsequent assistant messages (API returns 400 otherwise).
    - ``max_tokens`` covers both CoT reasoning AND final output, so we
      auto-bump it to at least 16384.
    - Temperature / top_p / penalties are ignored by the API.
    """

    # Minimum max_tokens when thinking is on (CoT + output share the budget)
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

    def _is_thinking_tier(self, model_tier: str) -> bool:
        """Whether this tier should use thinking mode."""
        if not self.settings.DEEPSEEK_THINKING_ENABLED:
            return False
        # Enable thinking only for balanced tier (not fast)
        return model_tier in ("balanced", self._models.get("balanced", ""))

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

        # --- Convert tools ---
        openai_tools = []
        for tool in tools:
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
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": json_lib.loads(tc.function.arguments),
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
        return True

    def supports_prompt_caching(self) -> bool:
        return False


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

