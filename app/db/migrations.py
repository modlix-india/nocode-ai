"""
Database migration runner (Flyway-style).

Reads SQL migration files from the migrations/ directory and applies
them in version order. Tracks applied migrations in ai_tracking_migrations table.
"""

import os
import re
import logging
from pathlib import Path
from typing import List, Tuple, Optional

from app.db.connection import get_connection, is_pool_available

logger = logging.getLogger(__name__)

# Migration files directory (relative to project root)
MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "migrations"


def get_migration_files() -> List[Tuple[str, str, Path]]:
    """
    Get list of migration files sorted by version.

    Returns:
        List of tuples: (version, description, file_path)
        e.g., [("V1", "Initial AI Tracking", Path("...V1__Initial_AI_Tracking.sql"))]
    """
    if not MIGRATIONS_DIR.exists():
        logger.warning(f"Migrations directory not found: {MIGRATIONS_DIR}")
        return []

    migrations = []
    pattern = re.compile(r"^(V\d+)__(.+)\.sql$")

    for file_path in MIGRATIONS_DIR.glob("*.sql"):
        match = pattern.match(file_path.name)
        if match:
            version = match.group(1)
            description = match.group(2).replace("_", " ")
            migrations.append((version, description, file_path))

    # Sort by version number
    migrations.sort(key=lambda x: int(x[0][1:]))
    return migrations


async def ensure_migrations_table() -> None:
    """
    Ensure the migrations tracking table exists.
    """
    async with get_connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS `ai_tracking_migrations` (
                    `ID` INT NOT NULL AUTO_INCREMENT,
                    `VERSION` VARCHAR(50) NOT NULL,
                    `DESCRIPTION` VARCHAR(200) NOT NULL,
                    `APPLIED_AT` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`ID`),
                    UNIQUE KEY `UK_VERSION` (`VERSION`)
                ) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)


async def get_applied_migrations() -> List[str]:
    """
    Get list of already applied migration versions.

    Returns:
        List of version strings, e.g., ["V1", "V2"]
    """
    async with get_connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT VERSION FROM ai_tracking_migrations ORDER BY ID"
            )
            result = await cursor.fetchall()
            return [row[0] for row in result]


async def apply_migration(version: str, description: str, file_path: Path) -> bool:
    """
    Apply a single migration file.

    Args:
        version: Migration version (e.g., "V1")
        description: Migration description
        file_path: Path to SQL file

    Returns:
        True if successful, False otherwise
    """
    try:
        # Read SQL file
        sql_content = file_path.read_text(encoding="utf-8")

        # Split by semicolons to execute multiple statements
        # Filter out empty statements and comments-only statements
        statements = []
        for stmt in sql_content.split(";"):
            # Remove comment lines and clean up
            lines = []
            for line in stmt.split("\n"):
                stripped = line.strip()
                # Skip empty lines and comment-only lines
                if stripped and not stripped.startswith("--"):
                    lines.append(line)

            clean_stmt = "\n".join(lines).strip()
            if clean_stmt:
                statements.append(clean_stmt)

        logger.info(f"Migration {version} has {len(statements)} statements to execute")

        async with get_connection() as conn:
            async with conn.cursor() as cursor:
                # Execute each statement
                for i, stmt in enumerate(statements):
                    if stmt:
                        # Log first 80 chars of statement for debugging
                        stmt_preview = stmt[:80].replace('\n', ' ')
                        logger.info(f"  [{i+1}/{len(statements)}] {stmt_preview}...")
                        try:
                            await cursor.execute(stmt)
                            logger.info(f"  [{i+1}/{len(statements)}] OK")
                        except Exception as e:
                            # Skip "database exists" and "table exists" errors
                            error_msg = str(e).lower()
                            if "database exists" in error_msg or "already exists" in error_msg:
                                logger.debug(f"Skipping already existing object: {e}")
                                continue
                            logger.error(f"Statement {i+1} failed: {stmt_preview}...")
                            logger.error(f"Error: {e}")
                            raise

                # Ensure changes are committed
                await conn.commit()
                logger.info(f"Migration {version} statements committed")

                # Record migration as applied
                await cursor.execute(
                    "INSERT INTO ai_tracking_migrations (VERSION, DESCRIPTION) VALUES (%s, %s)",
                    (version, description)
                )
                await conn.commit()

        logger.info(f"Applied migration {version}: {description}")
        return True

    except Exception as e:
        logger.error(f"Failed to apply migration {version}: {e}")
        return False


async def run_migrations() -> bool:
    """
    Run all pending migrations.

    Returns:
        True if all migrations successful, False otherwise
    """
    if not is_pool_available():
        logger.warning("Database pool not available, skipping migrations")
        return False

    logger.info("Running database migrations...")

    # Ensure migrations table exists
    await ensure_migrations_table()

    # Get migration files and applied versions
    migration_files = get_migration_files()
    applied_versions = await get_applied_migrations()

    if not migration_files:
        logger.info("No migration files found")
        return True

    logger.info(f"Found {len(migration_files)} migration files, {len(applied_versions)} already applied")

    # Apply pending migrations
    pending_count = 0
    success_count = 0

    for version, description, file_path in migration_files:
        if version not in applied_versions:
            pending_count += 1
            logger.info(f"Applying migration {version}: {description}")

            if await apply_migration(version, description, file_path):
                success_count += 1
            else:
                logger.error(f"Migration {version} failed, stopping")
                return False

    if pending_count == 0:
        logger.info("Database is up to date, no migrations needed")
    else:
        logger.info(f"Applied {success_count}/{pending_count} migrations successfully")

    return success_count == pending_count
