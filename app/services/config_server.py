"""Spring Cloud Config Server integration"""
import httpx
import logging
import os
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class ConfigServerClient:
    """
    Client for Spring Cloud Config Server.
    
    Fetches configuration from the config server at startup.
    Config server endpoint: http://{host}:{port}/{application}/{profile}
    """
    
    def __init__(
        self,
        url: str = "http://localhost:8888",
        application: str = "ai",
        profile: str = "default"
    ):
        self.url = url.rstrip("/")
        self.application = application
        self.profile = profile
        self._config: Dict[str, Any] = {}
    
    async def fetch_config(self) -> Dict[str, Any]:
        """
        Fetch configuration from config server.
        
        URL format: http://{host}:{port}/{application}/{profile}
        Examples:
          - http://localhost:8888/ai/default
          - http://localhost:8888/ai/dev
          - http://localhost:8888/ai/stage
          - http://localhost:8888/ai/prod
        
        Returns the 'ai' section of the configuration.
        """
        config_url = f"{self.url}/{self.application}/{self.profile}"
        logger.info(f"Fetching config from: {config_url} (profile: {self.profile})")
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(config_url)
                
                if response.status_code != 200:
                    logger.warning(
                        f"Config server returned {response.status_code}. "
                        "Using environment variables."
                    )
                    return {}
                
                data = response.json()
                
                # Config server returns a structure like:
                # {
                #   "name": "ai",
                #   "profiles": ["default"],
                #   "propertySources": [
                #     {
                #       "name": "...",
                #       "source": { "ai.security.url": "...", ... }
                #     }
                #   ]
                # }
                
                config = {}
                for prop_source in data.get("propertySources", []):
                    source = prop_source.get("source", {})
                    for key, value in source.items():
                        # Get ai.* properties (service-specific)
                        if key.startswith("ai."):
                            # Convert "ai.security.url" to nested dict
                            parts = key.split(".")
                            self._set_nested(config, parts[1:], value)
                        # Also get redis.* properties (shared infrastructure)
                        elif key.startswith("redis."):
                            # Convert "redis.url" to nested dict under "redis" key
                            parts = key.split(".")
                            self._set_nested(config, parts, value)
                
                self._config = config
                logger.info(f"Loaded config from server: {list(config.keys())}")
                return config
                
        except httpx.RequestError as e:
            logger.warning(f"Could not connect to config server: {e}")
            return {}
        except Exception as e:
            logger.error(f"Error fetching config: {e}")
            return {}
    
    def _set_nested(self, d: dict, keys: list, value):
        """Set a nested dictionary value from a list of keys"""
        for key in keys[:-1]:
            d = d.setdefault(key, {})
        d[keys[-1]] = value
    
    def get(self, key: str, default=None):
        """Get a config value using dot notation (e.g., 'security.url')"""
        parts = key.split(".")
        value = self._config
        try:
            for part in parts:
                value = value[part]
            return value
        except (KeyError, TypeError):
            return default


# Global config client instance
_config_client: Optional[ConfigServerClient] = None


async def initialize_config_from_server() -> Dict[str, Any]:
    """
    Initialize configuration from config server.
    
    Uses CLOUD_CONFIG_SERVER (hostname) and CONFIG_SERVER_PORT to build URL.
    Uses SPRING_PROFILES_ACTIVE for the profile (matches Java services convention).
    
    Called during application startup.
    """
    global _config_client
    
    config_host = os.getenv("CLOUD_CONFIG_SERVER", "localhost")
    config_port = os.getenv("CONFIG_SERVER_PORT", "8888")
    config_enabled = os.getenv("CONFIG_SERVER_ENABLED", "true").lower() == "true"
    profile = os.getenv("SPRING_PROFILES_ACTIVE", "default")
    
    # Build URL from host and port
    config_url = f"http://{config_host}:{config_port}"
    
    if not config_enabled:
        logger.info("Config server disabled, using environment variables")
        return {}
    
    _config_client = ConfigServerClient(
        url=config_url,
        application="ai",
        profile=profile
    )
    
    return await _config_client.fetch_config()


def get_config_client() -> Optional[ConfigServerClient]:
    """Get the config client instance"""
    return _config_client

