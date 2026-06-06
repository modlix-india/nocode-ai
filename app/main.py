"""
Nocode AI Service - FastAPI Application

Agentic AI service for building no-code applications through conversation.
Integrates with nocode-saas via Eureka service discovery and Config Server.
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

from app.config import settings, initialize_settings
from app.services.eureka import register_with_eureka, deregister_from_eureka
from app.services.redis_client import get_redis_client, close_redis
from app.api.routes import health
from app.middleware.rate_limiter import RateLimitMiddleware, RequestDeduplicationMiddleware

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler.

    Startup:
    1. Fetch config from Config Server
    2. Register with Eureka
    3. Initialize AppBuilder Agent

    Shutdown:
    - Close connections and deregister from Eureka
    """
    logger.info("=" * 60)
    logger.info("Starting Nocode AI Service")
    logger.info("=" * 60)

    # 1. Initialize settings from Config Server
    logger.info("Fetching configuration from Config Server...")
    await initialize_settings()

    # 2. Register with Eureka
    await register_with_eureka()

    # 3. Initialize Redis (if configured)
    if settings.REDIS_ENABLED:
        logger.info("Initializing Redis connection...")
        redis_client = await get_redis_client()
        if redis_client:
            logger.info("Redis connection established")
        else:
            logger.warning("Redis connection failed - rate limiting and caching disabled")

    # 4. Initialize AI Tracking Database (if configured)
    if settings.AI_TRACKING_ENABLED:
        logger.info("Initializing AI tracking database...")
        try:
            from app.db.connection import init_db_pool
            from app.db.migrations import run_migrations
            await init_db_pool()
            await run_migrations()
            logger.info("AI tracking database initialized")
        except Exception as e:
            logger.error(f"Failed to initialize AI tracking database: {e}")
            logger.warning("AI tracking will be disabled")
            settings.AI_TRACKING_ENABLED = False

    # 5. Initialize AppBuilder Agent (agentic system)
    logger.info("Initializing AppBuilder Agent...")
    try:
        from app.agents.appbuilder.context import build_appbuilder_context
        from app.agents.appbuilder.agent import AppBuilderAgent
        from app.agents.appbuilder.catalog import ComponentCatalog, set_catalog
        from app.agents.appbuilder.api_catalog import ApiCatalog
        from app.agents.appbuilder.tools.registry import ALL_TOOLS
        from app.agents.appbuilder.router import set_appbuilder_agent

        logger.info("Loading appbuilder context ...")
        appbuilder_context = build_appbuilder_context()
        await appbuilder_context.load()
        logger.info("Appbuilder context loaded")

        logger.info("Loading component catalog (URL=%s) ...", settings.COMPONENT_CATALOG_URL or "(fallback)")
        catalog = ComponentCatalog(settings.COMPONENT_CATALOG_URL)
        await catalog.load()
        set_catalog(catalog)  # register module-level singleton for tool helpers
        logger.info("Component catalog loaded: %d types", len(catalog.get_all_types()))

        logger.info("Loading API catalog ...")
        api_catalog = ApiCatalog()
        await api_catalog.load()
        from app.agents.appbuilder.tools.api_catalog_tools import set_api_catalog
        set_api_catalog(api_catalog)
        logger.info("API catalog loaded")

        logger.info("Creating AppBuilderAgent (provider=%s, model_tier=%s, max_turns=%d) ...",
                     settings.APPBUILDER_PROVIDER, settings.AGENT_MODEL_TIER, settings.MAX_AGENT_TURNS)
        appbuilder_agent = AppBuilderAgent(
            context_builder=appbuilder_context,
            tools=ALL_TOOLS,
            catalog=catalog,
            api_catalog=api_catalog,
            provider=settings.APPBUILDER_PROVIDER,
        )
        set_appbuilder_agent(appbuilder_agent)
        logger.info(f"AppBuilder Agent initialized with {len(ALL_TOOLS)} tools, {len(catalog.get_all_types())} component types")
    except Exception as e:
        logger.exception("Failed to initialize AppBuilder Agent")
        logger.warning("AppBuilder Agent will be unavailable")

    logger.info("=" * 60)
    logger.info(f"Service ready on port {settings.SERVICE_PORT}")
    logger.info("=" * 60)

    yield

    # Shutdown
    logger.info("Shutting down...")

    # Close SaasClient (HTTP connection pool)
    try:
        from app.agents.appbuilder.tools._shared import close_saas_client
        await close_saas_client()
    except Exception as e:
        logger.error(f"Error closing SaasClient: {e}")


    await close_redis()

    # Close AI tracking database connection
    if settings.AI_TRACKING_ENABLED:
        try:
            from app.db.connection import close_db_pool
            await close_db_pool()
        except Exception as e:
            logger.error(f"Error closing database pool: {e}")

    await deregister_from_eureka()
    logger.info("Shutdown complete")


# Create FastAPI application
app = FastAPI(
    title="Nocode AI Service",
    description="""
Agentic AI service for building no-code applications through conversation.

## Features

- **AppBuilder Agent**: Single agent with 61+ tools for building entire applications
- **SSE Streaming**: Real-time text + tool call progress via Server-Sent Events
- **Component Catalog**: Dynamic component metadata from UI source files

## Authentication

All endpoints require Bearer token authentication via the security service.
""",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/api/ai/docs",
    redoc_url="/api/ai/redoc",
    openapi_url="/api/ai/openapi.json"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add rate limiting middleware (uses Redis backend)
app.add_middleware(RateLimitMiddleware)

# Add request deduplication middleware (prevents duplicate concurrent requests)
app.add_middleware(RequestDeduplicationMiddleware)

# API prefix - matches gateway routing: /api/ai/**
API_PREFIX = "/api/ai"

# Include routers with /api/ai prefix to match gateway routing
app.include_router(health.router, prefix=API_PREFIX, tags=["Health"])

# AppBuilder agent router
from app.agents.appbuilder.router import router as appbuilder_router
app.include_router(appbuilder_router, prefix=f"{API_PREFIX}/appbuilder", tags=["AppBuilder"])

# Adzump agent router
from app.agents.adzump.router import router as adzump_router
app.include_router(adzump_router, prefix=f"{API_PREFIX}/adzump", tags=["Adzump"])

# Learning loop router (feedback, analytics, knowledge)
from app.learning.router import router as learning_router
app.include_router(learning_router, prefix=f"{API_PREFIX}/learning", tags=["Learning"])

# Admin: per-app KB export/import (cross-env promotion). Guarded by X-Admin-Token.
# Prefix is set on the router itself (/api/ai/admin/app-kb), so no extra prefix here.
from app.api.admin_app_kb import router as admin_app_kb_router
app.include_router(admin_app_kb_router)


# Root health check (for direct container health checks)
@app.get("/health")
async def root_health():
    """Root health check for container/load balancer"""
    return {"status": "UP", "service": "ai"}


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "Nocode AI Service",
        "version": "2.0.0",
        "port": settings.SERVICE_PORT,
        "endpoints": {
            "health": "/api/ai/health",
            "appbuilder_chat": "/api/ai/appbuilder/chat",
            "adzump_chat": "/api/ai/adzump/chat",
            "docs": "/api/ai/docs"
        }
    }


@app.get("/api/ai")
async def api_root():
    """API root endpoint"""
    return {
        "service": "Nocode AI Service",
        "version": "2.0.0",
        "endpoints": {
            "health": "/api/ai/health",
            "appbuilder_chat": "/api/ai/appbuilder/chat",
            "adzump_chat": "/api/ai/adzump/chat"
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.SERVICE_PORT,
        reload=True
    )
