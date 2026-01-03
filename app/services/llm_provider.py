"""
LLM Provider abstraction for supporting multiple LLM backends.

Supports:
- Anthropic (Claude): claude-haiku-4-5, claude-sonnet-4
- OpenAI (GPT): gpt-4o-mini, gpt-4o

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
        return self._models.get(tier, self._models["balanced"])
    
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
        return self._models.get(tier, self._models["balanced"])
    
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


# Singleton provider instance
_provider: Optional[LLMProvider] = None


def get_llm_provider() -> LLMProvider:
    """
    Get the configured LLM provider.
    
    Uses LLM_PROVIDER setting to determine which provider to use.
    Caches the provider instance for reuse.
    
    Returns:
        LLMProvider instance (AnthropicProvider or OpenAIProvider)
    """
    global _provider
    
    if _provider is not None:
        return _provider
    
    from app.config import settings
    
    if settings.LLM_PROVIDER.lower() == "openai":
        if not settings.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        _provider = OpenAIProvider()
        logger.info(f"Using OpenAI provider with models: {settings.OPENAI_MODEL_FAST}, {settings.OPENAI_MODEL_BALANCED}")
    else:
        if not settings.ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")
        _provider = AnthropicProvider()
        logger.info(f"Using Anthropic provider with models: {settings.CLAUDE_HAIKU}, {settings.CLAUDE_SONNET}")
    
    return _provider


def reset_provider():
    """Reset the provider singleton (useful for testing)"""
    global _provider
    _provider = None

