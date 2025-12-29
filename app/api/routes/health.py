"""Health check endpoint for Eureka and load balancers"""
from fastapi import APIRouter
from app.config import settings

router = APIRouter()


@router.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "UP", "service": "ai"}


@router.get("/health/detailed")
async def detailed_health_check():
    """
    Detailed health check including dependencies.
    
    Includes Redis and RAG engine status.
    """
    from app.services.redis_client import redis_health_check
    
    # Check Redis
    redis_status = await redis_health_check()
    
    # Overall status - UP if core service works, even if Redis is down
    overall_status = "UP"
    
    return {
        "status": overall_status,
        "service": "ai",
        "components": {
            "redis": redis_status,
            "promptCaching": {
                "enabled": settings.PROMPT_CACHING_ENABLED
            },
            "rateLimit": {
                "perMinute": settings.RATE_LIMIT_PER_MINUTE,
                "perHour": settings.RATE_LIMIT_PER_HOUR,
                "redisBackend": settings.REDIS_ENABLED
            }
        }
    }


@router.get("/info")
async def info():
    """Service info endpoint"""
    from app import __version__
    return {
        "service": "ai",
        "version": __version__,
        "description": "Nocode AI Service - Multi-agent page generation",
        "scaling": {
            "promptCaching": settings.PROMPT_CACHING_ENABLED,
            "redisEnabled": settings.REDIS_ENABLED,
            "rateLimitPerMinute": settings.RATE_LIMIT_PER_MINUTE,
            "rateLimitPerHour": settings.RATE_LIMIT_PER_HOUR
        }
    }

