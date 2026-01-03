"""
Database package for AI tracking.

This package provides:
- MySQL connection pool management
- Database migrations (Flyway-style)
- Pydantic models for database entities
"""

from app.db.connection import (
    init_db_pool,
    close_db_pool,
    get_connection,
    parse_jdbc_url,
)
from app.db.migrations import run_migrations
from app.db.models import (
    AiSession,
    AiSessionCreate,
    AiTokenUsage,
    AiTokenUsageCreate,
    AiSessionHistory,
    AiSessionHistoryCreate,
    ContextUsage,
)

__all__ = [
    # Connection
    "init_db_pool",
    "close_db_pool",
    "get_connection",
    "parse_jdbc_url",
    # Migrations
    "run_migrations",
    # Models
    "AiSession",
    "AiSessionCreate",
    "AiTokenUsage",
    "AiTokenUsageCreate",
    "AiSessionHistory",
    "AiSessionHistoryCreate",
    "ContextUsage",
]
