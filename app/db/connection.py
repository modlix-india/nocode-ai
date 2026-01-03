"""
MySQL connection pool for AI tracking.

Provides async connection management using aiomysql.
Parses JDBC URLs from config server to extract connection parameters.
"""

import logging
import re
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager
from urllib.parse import urlparse, parse_qs

import aiomysql

from app.config import settings

logger = logging.getLogger(__name__)

# Global connection pool
_pool: Optional[aiomysql.Pool] = None


def parse_jdbc_url(jdbc_url: str) -> Dict[str, Any]:
    """
    Parse a JDBC URL to extract connection parameters.

    Example: jdbc:mysql://localhost:3306/ai?serverTimezone=UTC
    Returns: {"host": "localhost", "port": 3306, "database": "ai", "params": {...}}
    """
    # Remove jdbc:mysql:// prefix
    url = jdbc_url
    if url.startswith("jdbc:mysql://"):
        url = url.replace("jdbc:mysql://", "mysql://")
    elif url.startswith("jdbc:"):
        url = url[5:]  # Remove "jdbc:" prefix

    # Parse URL
    parsed = urlparse(url)

    # Extract parameters from query string
    params = {}
    if parsed.query:
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 3306,
        "database": parsed.path.lstrip("/") if parsed.path else "ai",
        "params": params
    }


async def init_db_pool() -> None:
    """
    Initialize the MySQL connection pool.

    Should be called during application startup.
    """
    global _pool

    if not settings.MYSQL_URL:
        logger.warning("MYSQL_URL not configured, AI tracking will be disabled")
        return

    try:
        # Parse JDBC URL
        url_parts = parse_jdbc_url(settings.MYSQL_URL)

        logger.info(f"Connecting to MySQL: {url_parts['host']}:{url_parts['port']}/{url_parts['database']}")

        _pool = await aiomysql.create_pool(
            host=url_parts["host"],
            port=url_parts["port"],
            user=settings.MYSQL_USERNAME,
            password=settings.MYSQL_PASSWORD,
            db=url_parts["database"],
            charset="utf8mb4",
            autocommit=True,
            minsize=1,
            maxsize=10,
            pool_recycle=3600,  # Recycle connections after 1 hour
        )

        logger.info("MySQL connection pool initialized successfully")

    except Exception as e:
        logger.error(f"Failed to initialize MySQL connection pool: {e}")
        raise


async def close_db_pool() -> None:
    """
    Close the MySQL connection pool.

    Should be called during application shutdown.
    """
    global _pool

    if _pool:
        _pool.close()
        await _pool.wait_closed()
        _pool = None
        logger.info("MySQL connection pool closed")


@asynccontextmanager
async def get_connection():
    """
    Get a connection from the pool.

    Usage:
        async with get_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT * FROM ...")
                result = await cursor.fetchall()
    """
    global _pool

    if not _pool:
        raise RuntimeError("Database pool not initialized. Call init_db_pool() first.")

    conn = await _pool.acquire()
    try:
        yield conn
    finally:
        _pool.release(conn)


async def execute_query(query: str, params: tuple = None) -> Any:
    """
    Execute a query and return results.

    Args:
        query: SQL query string
        params: Query parameters (optional)

    Returns:
        Query results for SELECT, or affected row count for INSERT/UPDATE/DELETE
    """
    async with get_connection() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cursor:
            await cursor.execute(query, params)

            # For SELECT queries, fetch results
            if query.strip().upper().startswith("SELECT"):
                return await cursor.fetchall()

            # For INSERT, return lastrowid
            if query.strip().upper().startswith("INSERT"):
                return cursor.lastrowid

            # For UPDATE/DELETE, return rowcount
            return cursor.rowcount


async def execute_many(query: str, params_list: list) -> int:
    """
    Execute a query with multiple parameter sets.

    Args:
        query: SQL query string
        params_list: List of parameter tuples

    Returns:
        Total affected row count
    """
    async with get_connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.executemany(query, params_list)
            return cursor.rowcount


def is_pool_available() -> bool:
    """Check if the connection pool is available."""
    return _pool is not None
