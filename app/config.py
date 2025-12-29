"""Configuration settings for the AI service"""
import os
import logging
from pydantic_settings import BaseSettings
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """
    Application settings.
    
    Priority (highest to lowest):
    1. Environment variables
    2. Config server values (loaded at startup)
    3. Default values
    """
    
    # Service Identity
    SERVICE_NAME: str = "ai"
    SERVICE_PORT: int = 5001  # Changed to 5001
    
    # Eureka Service Discovery
    EUREKA_SERVER: str = "http://localhost:9999/eureka/"
    EUREKA_INSTANCE_HOST: str = "localhost"
    EUREKA_ENABLED: bool = True
    
    # Config Server
    # CLOUD_CONFIG_SERVER is the hostname (e.g., "config-server" in docker-compose)
    # Profile determines which config to fetch: ai/default, ai/ocidev, ai/ocistage, ai/ociprod
    CLOUD_CONFIG_SERVER: str = "localhost"
    CONFIG_SERVER_PORT: int = 8888
    CONFIG_SERVER_ENABLED: bool = True
    SPRING_PROFILES_ACTIVE: str = "default"  # Options: default, ocidev, ocistage, ociprod
    
    # Security Service (for token validation)
    # Can be overridden by config server: ai.security.url
    SECURITY_SERVICE_URL: str = "http://localhost:8080"
    
    # Redis (from config server: redis.url)
    # Used for rate limiting, request caching, and deduplication
    REDIS_URL: str = ""
    REDIS_ENABLED: bool = False  # Auto-enabled when URL is provided
    
    # Rate Limiting
    RATE_LIMIT_PER_MINUTE: int = 10  # Requests per minute per user
    RATE_LIMIT_PER_HOUR: int = 100  # Requests per hour per user
    
    # AI APIs
    # Can be overridden by config server: ai.secrets.anthropicAPIKey
    ANTHROPIC_API_KEY: str = ""
    
    # Multi-Model Strategy:
    # - HAIKU: Fast, cheap - for analysis, planning, simple tasks (styles, animations)
    # - SONNET: Balanced - for complex generation (components, events, review)
    CLAUDE_HAIKU: str = "claude-haiku-4-5-20251101"
    CLAUDE_SONNET: str = "claude-sonnet-4-20250514"
    
    # Anthropic Prompt Caching
    # Reduces token usage by ~90% for repeated system prompts
    PROMPT_CACHING_ENABLED: bool = True
    
    # Legacy - kept for backward compatibility
    CLAUDE_MODEL: str = "claude-sonnet-4-20250514"
    
    # Embeddings - Local HuggingFace model (no API needed)
    LOCAL_EMBEDDING_MODEL: str = "BAAI/bge-small-en-v1.5"
    
    # ChromaDB
    CHROMA_PERSIST_DIR: str = "./data/chroma"
    
    # RAG Document Paths
    AICONTEXT_PATH: str = "../nocode-ui/ui-app/aicontext"
    APP_DEFINITIONS_PATH: str = "./definitions/app defs"
    SITE_DEFINITIONS_PATH: str = "./definitions/site defs"
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"
    
    def apply_config_server_values(self, config: Dict[str, Any]):
        """
        Apply values from config server.
        
        Config server provides:
        - ai.security.url -> SECURITY_SERVICE_URL
        - ai.secrets.anthropicAPIKey -> ANTHROPIC_API_KEY
        - redis.url -> REDIS_URL
        """
        if not config:
            return
        
        # Map config server keys to settings
        # Format: (nested_keys_tuple) -> attribute_name
        mappings = {
            ("security", "url"): "SECURITY_SERVICE_URL",
            ("secrets", "anthropicAPIKey"): "ANTHROPIC_API_KEY",
        }
        
        for keys, attr in mappings.items():
            value = config
            try:
                for key in keys:
                    value = value[key]
                
                # Only apply if not already set via environment
                env_value = os.getenv(attr)
                if not env_value:
                    setattr(self, attr, value)
                    logger.info(f"Applied config server value for {attr}")
            except (KeyError, TypeError):
                pass
        
        # Special handling for Redis URL (top-level in config)
        # Config structure: redis: { url: "redis://..." }
        try:
            redis_url = config.get("redis", {}).get("url")
            if redis_url and not os.getenv("REDIS_URL"):
                self.REDIS_URL = redis_url
                self.REDIS_ENABLED = True
                logger.info("Applied config server value for REDIS_URL")
        except (KeyError, TypeError, AttributeError):
            pass


# Global settings instance
settings = Settings()


async def initialize_settings():
    """
    Initialize settings from config server.
    
    Should be called during application startup.
    """
    from app.services.config_server import initialize_config_from_server
    
    if settings.CONFIG_SERVER_ENABLED:
        config = await initialize_config_from_server()
        settings.apply_config_server_values(config)
    
    # Log final configuration (mask sensitive values)
    logger.info(f"Service: {settings.SERVICE_NAME}")
    logger.info(f"Port: {settings.SERVICE_PORT}")
    logger.info(f"Security URL: {settings.SECURITY_SERVICE_URL}")
    logger.info(f"Anthropic API Key: {'*' * 20 + settings.ANTHROPIC_API_KEY[-8:] if settings.ANTHROPIC_API_KEY else 'NOT SET'}")
    logger.info(f"Embedding Model: {settings.LOCAL_EMBEDDING_MODEL}")
    logger.info(f"Redis: {'ENABLED - ' + settings.REDIS_URL[:30] + '...' if settings.REDIS_ENABLED else 'DISABLED'}")
    logger.info(f"Prompt Caching: {'ENABLED' if settings.PROMPT_CACHING_ENABLED else 'DISABLED'}")
    logger.info(f"Rate Limit: {settings.RATE_LIMIT_PER_MINUTE}/min, {settings.RATE_LIMIT_PER_HOUR}/hour")
