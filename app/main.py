"""
Nocode AI Service - FastAPI Application

Multi-agent page generation with RAG support.
Integrates with nocode-saas via Eureka service discovery and Config Server.
"""
import os
# Suppress tokenizers parallelism warning (must be before importing transformers/tokenizers)
os.environ["TOKENIZERS_PARALLELISM"] = "false"

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

from app.config import settings, initialize_settings
from app.services.eureka import register_with_eureka, deregister_from_eureka
from app.services.redis_client import get_redis_client, close_redis
from app.rag.engine import initialize_rag_engine
from app.agents.page_agent import PageAgent
from app.api.routes import health, agent, query
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
    3. Initialize RAG engine
    4. Initialize Page Agent
    
    Shutdown:
    - Deregister from Eureka
    """
    logger.info("=" * 60)
    logger.info("Starting Nocode AI Service")
    logger.info("=" * 60)
    
    # 1. Initialize settings from Config Server
    logger.info("Fetching configuration from Config Server...")
    await initialize_settings()
    
    # 2. Register with Eureka
    await register_with_eureka()
    
    # 3. Initialize RAG engine
    logger.info("Initializing RAG engine...")
    await initialize_rag_engine()
    
    # 4. Initialize Redis (if configured)
    if settings.REDIS_ENABLED:
        logger.info("Initializing Redis connection...")
        redis_client = await get_redis_client()
        if redis_client:
            logger.info("Redis connection established")
        else:
            logger.warning("Redis connection failed - rate limiting and caching disabled")
    
    # 5. Initialize Page Agent
    logger.info("Initializing Page Agent...")
    page_agent = PageAgent()
    agent.set_page_agent(page_agent)
    
    logger.info("=" * 60)
    logger.info(f"Service ready on port {settings.SERVICE_PORT}")
    logger.info("=" * 60)
    
    yield
    
    # Shutdown
    logger.info("Shutting down...")
    await close_redis()
    await deregister_from_eureka()
    logger.info("Shutdown complete")


# Create FastAPI application
app = FastAPI(
    title="Nocode AI Service",
    description="""
Multi-agent AI service for generating nocode page definitions.

## Features

- **Page Agent**: Orchestrates 7 specialized sub-agents to generate pages
- **SSE Streaming**: Real-time progress updates during generation
- **RAG System**: Retrieval-augmented generation using documentation and examples
- **Create/Modify/Enhance**: Support for new pages and modifications
- **Config Server Integration**: Centralized configuration management

## Agents

| Agent | Responsibility |
|-------|---------------|
| Layout | Grid structure, responsive breakpoints |
| Component | Component selection and properties |
| Events | Event handlers and interactions |
| Styles | Visual styling and theming |
| Animation | Animations and transitions |
| Data | Data binding and store management |
| Review | Validation and quality improvement |

## Authentication

All endpoints require Bearer token authentication via the security service.
""",
    version="0.1.0",
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
app.include_router(agent.router, prefix=f"{API_PREFIX}/agent", tags=["Agents"])
app.include_router(query.router, prefix=f"{API_PREFIX}/query", tags=["Query"])


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
        "version": "0.1.0",
        "port": settings.SERVICE_PORT,
        "endpoints": {
            "health": "/api/ai/health",
            "page_generation": "/api/ai/agent/page",
            "page_generation_sync": "/api/ai/agent/page/sync",
            "query": "/api/ai/query",
            "docs": "/api/ai/docs"
        }
    }


@app.get("/api/ai")
async def api_root():
    """API root endpoint"""
    return {
        "service": "Nocode AI Service",
        "version": "0.1.0",
        "endpoints": {
            "health": "/api/ai/health",
            "page_generation": "/api/ai/agent/page",
            "page_generation_sync": "/api/ai/agent/page/sync",
            "query": "/api/ai/query"
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
