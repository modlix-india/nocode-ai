"""Redis client for caching, rate limiting, and request deduplication"""
import redis.asyncio as redis
import json
import hashlib
import logging
from typing import Optional, Any, Dict
from datetime import timedelta
from app.config import settings

logger = logging.getLogger(__name__)

# Global Redis connection pool
_redis_client: Optional[redis.Redis] = None


async def get_redis_client() -> Optional[redis.Redis]:
    """
    Get the Redis client instance.
    Uses connection pooling for efficiency.
    
    Returns:
        Redis client or None if Redis is not enabled/available
    """
    global _redis_client
    
    if not settings.REDIS_ENABLED or not settings.REDIS_URL:
        return None
    
    if _redis_client is None:
        try:
            _redis_client = redis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
                socket_timeout=5,
                socket_connect_timeout=5,
                retry_on_timeout=True
            )
            # Test connection
            await _redis_client.ping()
            logger.info("Redis connection established")
        except Exception as e:
            logger.warning(f"Failed to connect to Redis: {e}. Continuing without Redis.")
            _redis_client = None
            settings.REDIS_ENABLED = False
    
    return _redis_client


async def close_redis():
    """Close the Redis connection gracefully"""
    global _redis_client
    if _redis_client:
        await _redis_client.close()
        _redis_client = None
        logger.info("Redis connection closed")


# === Request Deduplication ===

def get_request_key(user_id: str, page_id: str, instruction: str) -> str:
    """Generate a unique key for request deduplication"""
    content = f"{user_id}:{page_id}:{instruction}"
    return f"ai:request:{hashlib.md5(content.encode()).hexdigest()}"


async def check_active_request(user_id: str, page_id: str, instruction: str) -> Optional[str]:
    """
    Check if there's already an active request for the same page/instruction.
    
    Returns:
        request_id if request is active, None otherwise
    """
    client = await get_redis_client()
    if not client:
        return None
    
    try:
        key = get_request_key(user_id, page_id, instruction)
        return await client.get(key)
    except Exception as e:
        logger.warning(f"Redis check_active_request error: {e}")
        return None


async def mark_request_active(
    user_id: str, 
    page_id: str, 
    instruction: str, 
    request_id: str,
    ttl_seconds: int = 300  # 5 minutes max for a request
) -> bool:
    """
    Mark a request as active (in progress).
    
    Returns:
        True if marked successfully, False if already active
    """
    client = await get_redis_client()
    if not client:
        return True  # Allow if Redis unavailable
    
    try:
        key = get_request_key(user_id, page_id, instruction)
        # SET NX (only if not exists)
        result = await client.set(key, request_id, nx=True, ex=ttl_seconds)
        return result is True
    except Exception as e:
        logger.warning(f"Redis mark_request_active error: {e}")
        return True


async def clear_request_active(user_id: str, page_id: str, instruction: str):
    """Remove the active request marker after completion"""
    client = await get_redis_client()
    if not client:
        return
    
    try:
        key = get_request_key(user_id, page_id, instruction)
        await client.delete(key)
    except Exception as e:
        logger.warning(f"Redis clear_request_active error: {e}")


# === Response Caching ===

def get_cache_key(page_id: str, instruction: str, mode: str) -> str:
    """Generate a cache key for response caching"""
    content = f"{page_id}:{instruction}:{mode}"
    return f"ai:cache:{hashlib.md5(content.encode()).hexdigest()}"


async def get_cached_response(
    page_id: str, 
    instruction: str, 
    mode: str
) -> Optional[Dict[str, Any]]:
    """
    Get cached response for identical request.
    
    Note: Caching is only useful for:
    - Demo/preview requests
    - Retries of failed requests
    
    Most real requests will have unique instructions.
    """
    client = await get_redis_client()
    if not client:
        return None
    
    try:
        key = get_cache_key(page_id, instruction, mode)
        cached = await client.get(key)
        if cached:
            logger.info(f"Cache hit for page {page_id}")
            return json.loads(cached)
    except Exception as e:
        logger.warning(f"Redis cache get error: {e}")
    
    return None


async def cache_response(
    page_id: str, 
    instruction: str, 
    mode: str,
    response: Dict[str, Any],
    ttl_seconds: int = 3600  # 1 hour cache
):
    """Cache a successful response"""
    client = await get_redis_client()
    if not client:
        return
    
    try:
        key = get_cache_key(page_id, instruction, mode)
        await client.set(key, json.dumps(response), ex=ttl_seconds)
        logger.debug(f"Cached response for page {page_id}")
    except Exception as e:
        logger.warning(f"Redis cache set error: {e}")


# === Rate Limiting Support ===
# Note: Rate limiting is handled by slowapi middleware,
# but we use Redis as the backend storage

async def get_rate_limit_count(user_id: str, window: str = "minute") -> int:
    """
    Get current request count for rate limiting.
    
    Args:
        user_id: User identifier
        window: "minute" or "hour"
    
    Returns:
        Current request count in the window
    """
    client = await get_redis_client()
    if not client:
        return 0
    
    try:
        key = f"ai:ratelimit:{user_id}:{window}"
        count = await client.get(key)
        return int(count) if count else 0
    except Exception as e:
        logger.warning(f"Redis rate limit get error: {e}")
        return 0


async def increment_rate_limit(user_id: str, window: str = "minute") -> int:
    """
    Increment request count for rate limiting.
    
    Returns:
        New count after increment
    """
    client = await get_redis_client()
    if not client:
        return 0
    
    try:
        key = f"ai:ratelimit:{user_id}:{window}"
        ttl = 60 if window == "minute" else 3600
        
        # INCR and set expiry
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.expire(key, ttl)
        results = await pipe.execute()
        return results[0]
    except Exception as e:
        logger.warning(f"Redis rate limit incr error: {e}")
        return 0


# === Health Check ===

async def redis_health_check() -> Dict[str, Any]:
    """
    Check Redis connection health.
    
    Returns:
        Health status dict
    """
    if not settings.REDIS_ENABLED:
        return {"status": "disabled", "message": "Redis not configured"}
    
    try:
        client = await get_redis_client()
        if client:
            await client.ping()
            info = await client.info("server")
            return {
                "status": "healthy",
                "version": info.get("redis_version", "unknown"),
                "uptime_seconds": info.get("uptime_in_seconds", 0)
            }
        return {"status": "unavailable", "message": "No connection"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

