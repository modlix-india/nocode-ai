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

# The three characters MySQL quotes with. A semicolon between a matched pair
# of these is data, not a statement terminator.
_QUOTE_CHARS = ("'", '"', "`")


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


def _split_statements(sql: str) -> List[str]:
    """Split SQL into statements on unquoted semicolons.

    Tracks the three MySQL quoting characters (`'`, `"`, backtick) and both
    escape forms inside them: a backslash escape (`\\'`) and a doubled quote
    (`''`). Everything else is passed through untouched — this is a splitter,
    not a parser, and it deliberately knows nothing about SQL grammar.
    """
    statements: List[str] = []
    buf: List[str] = []
    quote: Optional[str] = None
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            consumed, quote = _scan_quoted(sql, i, quote, buf)
            i += consumed
            continue
        if ch in _QUOTE_CHARS:
            quote = ch
            buf.append(ch)
        elif ch == ";":
            statements.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    statements.append("".join(buf))
    return [s.strip() for s in statements if s.strip()]


def _scan_quoted(
    sql: str, i: int, quote: str, buf: List[str]
) -> Tuple[int, Optional[str]]:
    """Consume one unit inside a quoted run. Returns (chars consumed, quote still open)."""
    ch = sql[i]
    nxt = sql[i + 1] if i + 1 < len(sql) else ""
    # Backslash escape: the next character is literal whatever it is.
    if ch == "\\" and nxt:
        buf.append(ch)
        buf.append(nxt)
        return 2, quote
    # A doubled quote is a literal quote, not the closing one.
    if ch == quote and nxt == quote:
        buf.append(ch)
        buf.append(nxt)
        return 2, quote
    buf.append(ch)
    return 1, (None if ch == quote else quote)


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

        # STRIP `-- ...` line comments FIRST so semicolons inside comments
        # don't split statements. The prior "split by ; then drop --" order
        # broke on `-- ... is the live state; prior versions ...` because
        # the semicolon-in-comment split happened before the line was
        # recognised as a comment, leaving orphaned text on the next stmt.
        comment_stripped_lines: list[str] = []
        for line in sql_content.split("\n"):
            if line.strip().startswith("--"):
                continue
            comment_stripped_lines.append(line)
        sql_no_comments = "\n".join(comment_stripped_lines)

        # Split on semicolons, but only ones that are actually statement
        # terminators. A plain `.split(";")` cuts through quoted text, and a
        # column COMMENT is quoted text people naturally write sentences in:
        #     COMMENT '0-100 at LAST_CONFIRMED_AT; decayed at read time'
        # split that way and MySQL gets half a CREATE TABLE plus a fragment,
        # so the whole migration fails and is never recorded — silently, since
        # the caller only logs. V13 (lore) shipped exactly that and its tables
        # were never created by the runner.
        statements = _split_statements(sql_no_comments)

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
                            # Re-running a migration must be harmless. CREATE
                            # says "already exists"; ALTER ... ADD COLUMN says
                            # "duplicate column name" and ADD KEY says
                            # "duplicate key name" — same situation, different
                            # wording, and neither has an IF NOT EXISTS form in
                            # MySQL 8. Treat all three as already-applied.
                            error_msg = str(e).lower()
                            if (
                                "database exists" in error_msg
                                or "already exists" in error_msg
                                or "duplicate column name" in error_msg
                                or "duplicate key name" in error_msg
                            ):
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
