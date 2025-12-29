"""Rate limiting middleware using Redis backend"""
import logging
from typing import Optional
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from app.config import settings
from app.services.redis_client import get_redis_client

logger = logging.getLogger(__name__)


def get_user_id(request: Request) -> str:
    """
    Extract user identifier from request.
    
    Priority:
    1. User ID from validated token (set by security middleware)
    2. X-User-Id header (from gateway)
    3. Remote IP address
    """
    # Check for user ID set by security validation
    if hasattr(request.state, "user_id"):
        return request.state.user_id
    
    # Check for X-User-Id header from gateway
    user_id = request.headers.get("X-User-Id")
    if user_id:
        return user_id
    
    # Fall back to IP address
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Rate limiting middleware for AI endpoints.
    
    Uses Redis for distributed rate limiting across multiple workers.
    Falls back to allowing requests if Redis is unavailable.
    """
    
    async def dispatch(self, request: Request, call_next) -> Response:
        # Only rate limit AI generation endpoints
        if not request.url.path.startswith("/api/ai/agent"):
            return await call_next(request)
        
        # Skip rate limiting for health checks
        if request.url.path.endswith("/health"):
            return await call_next(request)
        
        user_id = get_user_id(request)
        
        # Check Redis availability
        redis_client = await get_redis_client()
        if not redis_client:
            # Redis unavailable - allow request but log warning
            logger.debug("Rate limiting bypassed - Redis unavailable")
            return await call_next(request)
        
        try:
            # Check minute-level rate limit
            minute_key = f"ai:ratelimit:{user_id}:minute"
            minute_count = await redis_client.incr(minute_key)
            
            # Set expiry on first request in window
            if minute_count == 1:
                await redis_client.expire(minute_key, 60)
            
            if minute_count > settings.RATE_LIMIT_PER_MINUTE:
                logger.warning(f"Rate limit exceeded for user {user_id}: {minute_count}/minute")
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error": "Rate limit exceeded",
                        "message": f"Maximum {settings.RATE_LIMIT_PER_MINUTE} requests per minute. Please wait.",
                        "retry_after": 60
                    }
                )
            
            # Check hour-level rate limit
            hour_key = f"ai:ratelimit:{user_id}:hour"
            hour_count = await redis_client.incr(hour_key)
            
            if hour_count == 1:
                await redis_client.expire(hour_key, 3600)
            
            if hour_count > settings.RATE_LIMIT_PER_HOUR:
                logger.warning(f"Hourly rate limit exceeded for user {user_id}: {hour_count}/hour")
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error": "Hourly rate limit exceeded",
                        "message": f"Maximum {settings.RATE_LIMIT_PER_HOUR} requests per hour. Please try again later.",
                        "retry_after": 3600
                    }
                )
            
            # Add rate limit headers to response
            response = await call_next(request)
            response.headers["X-RateLimit-Limit-Minute"] = str(settings.RATE_LIMIT_PER_MINUTE)
            response.headers["X-RateLimit-Remaining-Minute"] = str(max(0, settings.RATE_LIMIT_PER_MINUTE - minute_count))
            response.headers["X-RateLimit-Limit-Hour"] = str(settings.RATE_LIMIT_PER_HOUR)
            response.headers["X-RateLimit-Remaining-Hour"] = str(max(0, settings.RATE_LIMIT_PER_HOUR - hour_count))
            
            return response
            
        except HTTPException:
            raise
        except Exception as e:
            # On Redis errors, allow the request but log
            logger.warning(f"Rate limit check failed: {e}")
            return await call_next(request)


class RequestDeduplicationMiddleware(BaseHTTPMiddleware):
    """
    Prevents duplicate concurrent requests from the same user.
    
    If a user submits the same request while one is in progress,
    returns a 409 Conflict instead of starting a duplicate.
    """
    
    async def dispatch(self, request: Request, call_next) -> Response:
        # Only deduplicate POST requests to generation endpoints
        if request.method != "POST" or not request.url.path.startswith("/api/ai/agent"):
            return await call_next(request)
        
        redis_client = await get_redis_client()
        if not redis_client:
            return await call_next(request)
        
        user_id = get_user_id(request)
        
        # Create a simple lock key based on user + endpoint
        lock_key = f"ai:active:{user_id}:{request.url.path}"
        
        try:
            # Try to acquire lock (NX = only if not exists, EX = expire after 300s)
            acquired = await redis_client.set(lock_key, "1", nx=True, ex=300)
            
            if not acquired:
                logger.info(f"Duplicate request blocked for user {user_id}")
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "Request already in progress",
                        "message": "Please wait for the current AI generation to complete."
                    }
                )
            
            try:
                response = await call_next(request)
                return response
            finally:
                # Release lock after request completes
                await redis_client.delete(lock_key)
                
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"Deduplication check failed: {e}")
            return await call_next(request)

