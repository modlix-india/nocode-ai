"""Eureka service registration"""
import logging
from app.config import settings

logger = logging.getLogger(__name__)


async def register_with_eureka():
    """Register service with Eureka server"""
    if not settings.EUREKA_ENABLED:
        logger.info("Eureka registration disabled")
        return
    
    try:
        import py_eureka_client.eureka_client as eureka_client
        
        await eureka_client.init_async(
            eureka_server=settings.EUREKA_SERVER,
            app_name=settings.SERVICE_NAME,
            instance_port=settings.SERVICE_PORT,
            instance_host=settings.EUREKA_INSTANCE_HOST,
            health_check_url=f"http://{settings.EUREKA_INSTANCE_HOST}:{settings.SERVICE_PORT}/health",
            renewal_interval_in_secs=30,
            duration_in_secs=90,
        )
        logger.info(f"Registered with Eureka: {settings.SERVICE_NAME} at {settings.EUREKA_SERVER}")
    except Exception as e:
        logger.error(f"Failed to register with Eureka: {e}")
        # Don't fail startup if Eureka is unavailable
        if settings.EUREKA_ENABLED:
            logger.warning("Continuing without Eureka registration")


async def deregister_from_eureka():
    """Deregister service from Eureka server"""
    if not settings.EUREKA_ENABLED:
        return
    
    try:
        import py_eureka_client.eureka_client as eureka_client
        await eureka_client.stop_async()
        logger.info("Deregistered from Eureka")
    except Exception as e:
        logger.error(f"Failed to deregister from Eureka: {e}")

