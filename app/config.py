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
    
    # Files Service (for image uploads)
    # Can be overridden by config server: ai.files.url
    FILES_SERVICE_URL: str = "http://localhost:8000"
    
    # Redis (from config server: redis.url)
    # Used for rate limiting, request caching, and deduplication
    REDIS_URL: str = ""
    REDIS_ENABLED: bool = False  # Auto-enabled when URL is provided
    
    # Rate Limiting
    RATE_LIMIT_PER_MINUTE: int = 10  # Requests per minute per user
    RATE_LIMIT_PER_HOUR: int = 100  # Requests per hour per user

    # MySQL Database for AI Tracking
    # Can be overridden by config server: ai.db.*
    MYSQL_URL: str = ""  # JDBC URL: jdbc:mysql://localhost:3306/ai?serverTimezone=UTC
    MYSQL_USERNAME: str = "root"
    MYSQL_PASSWORD: str = ""
    AI_TRACKING_ENABLED: bool = False  # Auto-enabled when MYSQL_URL is configured

    # Context limits for conversation tracking
    CONTEXT_LIMIT_DEFAULT: int = 184000  # Default context limit (200K - 16K reserved for output)
    
    # LLM Provider Selection
    # Options: "anthropic" or "openai"
    # Can be overridden by config server: ai.llm.provider
    LLM_PROVIDER: str = "anthropic"
    
    # Anthropic Settings
    # Can be overridden by config server: ai.secrets.anthropicAPIKey
    ANTHROPIC_API_KEY: str = ""
    CLAUDE_HAIKU: str = "claude-haiku-4-5-20251001"      # Fast model for analysis
    CLAUDE_SONNET: str = "claude-opus-4-5-20251101"      # Balanced model for generation
    
    # OpenAI Settings
    # Can be overridden by config server: ai.secrets.openaiAPIKey
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL_FAST: str = "gpt-4o-mini"    # Equivalent to Claude Haiku
    OPENAI_MODEL_BALANCED: str = "gpt-4o"      # Equivalent to Claude Sonnet
    
    # Prompt Caching (Anthropic-only feature)
    # Reduces token usage by ~90% for repeated system prompts
    # Automatically disabled when using OpenAI
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
    
    # Website Import Settings
    WEBSITE_IMPORT_TIMEOUT: int = 30  # Timeout for website HTML fetching (seconds)
    SCREENSHOT_TIMEOUT: int = 60  # Timeout for screenshot capture (seconds)
    MAX_HTML_SIZE_MB: int = 10  # Maximum HTML size to process (MB)
    PLACEHOLDER_IMAGE_PATH: str = "api/files/static/file/SYSTEM/appbuilder/sample.svg"  # Default placeholder image
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"
    
    def apply_config_server_values(self, config: Dict[str, Any]):
        """
        Apply values from config server.
        
        Config server provides:
        - ai.security.url -> SECURITY_SERVICE_URL
        - ai.files.url -> FILES_SERVICE_URL
        - ai.secrets.anthropicAPIKey -> ANTHROPIC_API_KEY
        - ai.secrets.openaiAPIKey -> OPENAI_API_KEY
        - ai.llm.provider -> LLM_PROVIDER
        - redis.url -> REDIS_URL
        """
        if not config:
            return
        
        # Map config server keys to settings
        # Format: (nested_keys_tuple) -> attribute_name
        mappings = {
            ("security", "url"): "SECURITY_SERVICE_URL",
            ("files", "url"): "FILES_SERVICE_URL",
            ("secrets", "anthropicAPIKey"): "ANTHROPIC_API_KEY",
            ("secrets", "openaiAPIKey"): "OPENAI_API_KEY",
            ("llm", "provider"): "LLM_PROVIDER",
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
        
        # Special handling for Redis URL
        # Priority: 1) ai.redis.url (ai-specific config), 2) redis.url (shared config)
        try:
            # First try ai-specific redis config
            redis_url = config.get("redis", {}).get("url")
            if not redis_url:
                # Fall back to top-level redis config (shared across services)
                # This is fetched separately from config server
                pass

            if redis_url and not os.getenv("REDIS_URL"):
                self.REDIS_URL = redis_url
                self.REDIS_ENABLED = True
                logger.info("Applied config server value for REDIS_URL")
        except (KeyError, TypeError, AttributeError):
            pass

        # Special handling for AI database config (under "db" key like other services)
        # Config structure: ai.db: { url: "jdbc:mysql://...", username: "...", password: "..." }
        try:
            db_config = config.get("db", {})
            if db_config.get("url") and not os.getenv("MYSQL_URL"):
                self.MYSQL_URL = db_config.get("url", "")
                self.MYSQL_USERNAME = db_config.get("username", "root")
                self.MYSQL_PASSWORD = db_config.get("password", "")
                self.AI_TRACKING_ENABLED = True
                logger.info("Applied config server values for AI database")
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
    logger.info(f"LLM Provider: {settings.LLM_PROVIDER.upper()}")
    logger.info(f"Security URL: {settings.SECURITY_SERVICE_URL}")
    logger.info(f"Files URL: {settings.FILES_SERVICE_URL}")
    
    if settings.LLM_PROVIDER == "anthropic":
        logger.info(f"Anthropic API Key: {'*' * 20 + settings.ANTHROPIC_API_KEY[-8:] if settings.ANTHROPIC_API_KEY else 'NOT SET'}")
        logger.info(f"Models: Haiku={settings.CLAUDE_HAIKU}, Sonnet={settings.CLAUDE_SONNET}")
        logger.info(f"Prompt Caching: {'ENABLED' if settings.PROMPT_CACHING_ENABLED else 'DISABLED'}")
    else:
        logger.info(f"OpenAI API Key: {'*' * 20 + settings.OPENAI_API_KEY[-8:] if settings.OPENAI_API_KEY else 'NOT SET'}")
        logger.info(f"Models: Fast={settings.OPENAI_MODEL_FAST}, Balanced={settings.OPENAI_MODEL_BALANCED}")
    
    logger.info(f"Embedding Model: {settings.LOCAL_EMBEDDING_MODEL}")
    logger.info(f"Redis: {'ENABLED - ' + settings.REDIS_URL[:30] + '...' if settings.REDIS_ENABLED else 'DISABLED'}")
    logger.info(f"Rate Limit: {settings.RATE_LIMIT_PER_MINUTE}/min, {settings.RATE_LIMIT_PER_HOUR}/hour")
    logger.info(f"AI Tracking: {'ENABLED - ' + settings.MYSQL_URL[:50] + '...' if settings.AI_TRACKING_ENABLED else 'DISABLED'}")
