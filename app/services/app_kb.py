"""Repository for the per-app knowledge base (cfa_app_kb).

Storage: MySQL, schema in migrations/V12__CFA_App_KB.sql.

This module is pure data access — no business policy (the propose-then-confirm
write flow lives in app/agents/appbuilder/tools/kb_app.py and uses these
primitives). Cross-env promotion (Phase 7) also calls into here directly.

Key invariants:
  - Latest version per (client, app, section) is the live state.
  - `decisions_log` is append-only: each commit gets a new version, never
    overwrites.
  - Version numbers are MONOTONIC per (client, app, section).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Iterable

from app.db.connection import execute_query

logger = logging.getLogger(__name__)


# The 6 typed sections plus a sentinel for "any". `decisions_log` is the
# append-only one; the rest are last-writer-wins on the (client, app, section)
# tuple with full history preserved by version.
SECTIONS: tuple[str, ...] = (
    "overview",
    "current_focus",
    "inventory",
    "conventions",
    "roadmap",
    "decisions_log",
)
APPEND_ONLY_SECTIONS: frozenset[str] = frozenset({"decisions_log"})


def body_hash(body: str) -> str:
    """SHA-256 hex digest of the body — used to short-circuit no-op writes."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def validate_section(section: str) -> str | None:
    if section not in SECTIONS:
        return f"Unknown section '{section}'. Valid: {', '.join(SECTIONS)}"
    return None


# ── Reads ───────────────────────────────────────────────────────────────


async def get_latest(client_code: str, app_code: str, section: str) -> dict[str, Any] | None:
    """Return the most recent row for a (client, app, section), or None."""
    rows = await execute_query(
        """SELECT ID, CLIENT_CODE, APP_CODE, SECTION, BODY, BODY_HASH, VERSION,
                  UPDATED_BY, UPDATED_AT, MESSAGE
             FROM cfa_app_kb
            WHERE CLIENT_CODE=%s AND APP_CODE=%s AND SECTION=%s
         ORDER BY VERSION DESC LIMIT 1""",
        (client_code, app_code, section),
    )
    return rows[0] if rows else None


async def get_version(
    client_code: str, app_code: str, section: str, version: int,
) -> dict[str, Any] | None:
    """Fetch one specific historical version."""
    rows = await execute_query(
        """SELECT ID, CLIENT_CODE, APP_CODE, SECTION, BODY, BODY_HASH, VERSION,
                  UPDATED_BY, UPDATED_AT, MESSAGE
             FROM cfa_app_kb
            WHERE CLIENT_CODE=%s AND APP_CODE=%s AND SECTION=%s AND VERSION=%s""",
        (client_code, app_code, section, version),
    )
    return rows[0] if rows else None


async def list_history(
    client_code: str, app_code: str, section: str, limit: int = 10,
) -> list[dict[str, Any]]:
    """Return the most recent `limit` versions of a section, newest first.

    For `decisions_log` this returns the full append-only timeline. For other
    sections it returns the live state plus prior revisions (useful for
    audit / rollback diagnosis).
    """
    limit = max(1, min(limit, 200))
    rows = await execute_query(
        """SELECT VERSION, UPDATED_BY, UPDATED_AT, MESSAGE, BODY_HASH
             FROM cfa_app_kb
            WHERE CLIENT_CODE=%s AND APP_CODE=%s AND SECTION=%s
         ORDER BY VERSION DESC
            LIMIT %s""",
        (client_code, app_code, section, limit),
    )
    return rows or []


async def list_sections_present(client_code: str, app_code: str) -> list[str]:
    """Which sections have at least one row for this (client, app)? Used by
    session bootstrap to decide which sections to read into the system prompt."""
    rows = await execute_query(
        """SELECT DISTINCT SECTION
             FROM cfa_app_kb
            WHERE CLIENT_CODE=%s AND APP_CODE=%s""",
        (client_code, app_code),
    )
    return sorted(r["SECTION"] for r in (rows or []))


async def search(
    client_code: str, app_code: str, query: str, limit: int = 10,
) -> list[dict[str, Any]]:
    """Full-text search across the latest version of every section for this app.

    Uses the FT_BODY FULLTEXT index. Returns matches with a relevance score so
    the agent can rank what to read first.
    """
    limit = max(1, min(limit, 50))
    rows = await execute_query(
        """SELECT k.SECTION, k.VERSION, k.UPDATED_AT, k.MESSAGE,
                  MATCH(k.BODY) AGAINST(%s IN NATURAL LANGUAGE MODE) AS score,
                  SUBSTRING(k.BODY, 1, 240) AS snippet
             FROM cfa_app_kb k
             JOIN (
                 SELECT SECTION, MAX(VERSION) AS V
                   FROM cfa_app_kb
                  WHERE CLIENT_CODE=%s AND APP_CODE=%s
               GROUP BY SECTION
             ) latest
               ON latest.SECTION = k.SECTION AND latest.V = k.VERSION
            WHERE k.CLIENT_CODE=%s AND k.APP_CODE=%s
              AND MATCH(k.BODY) AGAINST(%s IN NATURAL LANGUAGE MODE)
         ORDER BY score DESC
            LIMIT %s""",
        (query, client_code, app_code, client_code, app_code, query, limit),
    )
    return rows or []


# ── Writes ──────────────────────────────────────────────────────────────


async def next_version(client_code: str, app_code: str, section: str) -> int:
    """Compute the next monotonic version for this (client, app, section).

    Returns 1 if no prior rows exist.
    """
    rows = await execute_query(
        """SELECT COALESCE(MAX(VERSION), 0) + 1 AS next
             FROM cfa_app_kb
            WHERE CLIENT_CODE=%s AND APP_CODE=%s AND SECTION=%s""",
        (client_code, app_code, section),
    )
    return int(rows[0]["next"]) if rows else 1


async def insert_version(
    client_code: str,
    app_code: str,
    section: str,
    body: str,
    updated_by: int,
    message: str = "",
    *,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Insert a new version row for the (client, app, section).

    Optimistic-lock: if `expected_version` is set, the new row's `VERSION`
    will be `expected_version + 1`; the unique index on
    (client_code, app_code, section, version) makes this atomically fail when
    two callers race past the same expected version. Caller catches the
    IntegrityError and re-fetches.

    Returns the inserted row (with id + version filled in).
    """
    if expected_version is not None:
        new_version = expected_version + 1
    else:
        new_version = await next_version(client_code, app_code, section)

    digest = body_hash(body)
    last_id = await execute_query(
        """INSERT INTO cfa_app_kb
               (CLIENT_CODE, APP_CODE, SECTION, BODY, BODY_HASH, VERSION, UPDATED_BY, MESSAGE)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (client_code, app_code, section, body, digest, new_version, updated_by, message[:512] if message else None),
    )
    return {
        "id": last_id,
        "client_code": client_code,
        "app_code": app_code,
        "section": section,
        "version": new_version,
        "updated_by": updated_by,
        "body_hash": digest,
        "message": message,
    }


# ── Bulk operations for promotion / migration ────────────────────────────


async def export_app(client_code: str, app_code: str) -> dict[str, Any]:
    """Return a portable JSON snapshot of all rows for (client, app).

    Used by /admin/app-kb/export and by promote_app_kb.py. Includes the full
    history for decisions_log + latest version for the other sections (the
    minimum that preserves audit and replay across envs).
    """
    rows = await execute_query(
        """SELECT SECTION, BODY, BODY_HASH, VERSION, UPDATED_BY, UPDATED_AT, MESSAGE
             FROM cfa_app_kb
            WHERE CLIENT_CODE=%s AND APP_CODE=%s
         ORDER BY SECTION, VERSION""",
        (client_code, app_code),
    )
    return {
        "client_code": client_code,
        "app_code": app_code,
        "rows": rows or [],
    }


async def import_snapshot(
    snapshot: dict[str, Any],
    *,
    target_client: str,
    target_app: str,
    updated_by: int,
    promotion_note: str,
    skip_if_same: bool = True,
) -> dict[str, int]:
    """Apply an exported snapshot to the destination (client, app).

    For non-append-only sections: inserts ONE new version per section using the
    snapshot's latest body. Skips rows whose body_hash matches the current
    destination latest (no-op promotion). For decisions_log: inserts every
    snapshot row not already present (matched by body_hash), preserving order.

    Returns counts: {sections_inserted, decisions_added, skipped}.
    """
    counters = {"sections_inserted": 0, "decisions_added": 0, "skipped": 0}
    rows: Iterable[dict[str, Any]] = snapshot.get("rows") or []

    # Group source rows by section.
    by_section: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_section.setdefault(r["SECTION"], []).append(r)

    for section, src_rows in by_section.items():
        if section not in SECTIONS:
            counters["skipped"] += len(src_rows)
            continue
        if section in APPEND_ONLY_SECTIONS:
            # Add any decisions whose body_hash isn't already in destination.
            existing = await execute_query(
                """SELECT BODY_HASH FROM cfa_app_kb
                    WHERE CLIENT_CODE=%s AND APP_CODE=%s AND SECTION=%s""",
                (target_client, target_app, section),
            )
            seen = {r["BODY_HASH"] for r in (existing or [])}
            for src in src_rows:
                if src["BODY_HASH"] in seen:
                    counters["skipped"] += 1
                    continue
                await insert_version(
                    target_client, target_app, section, src["BODY"],
                    updated_by=updated_by, message=promotion_note,
                )
                counters["decisions_added"] += 1
            continue

        # Single-value section: take the latest row in the source snapshot.
        src_rows.sort(key=lambda r: r.get("VERSION") or 0)
        latest_src = src_rows[-1]
        if skip_if_same:
            existing = await get_latest(target_client, target_app, section)
            if existing and existing.get("BODY_HASH") == latest_src["BODY_HASH"]:
                counters["skipped"] += 1
                continue
        await insert_version(
            target_client, target_app, section, latest_src["BODY"],
            updated_by=updated_by, message=promotion_note,
        )
        counters["sections_inserted"] += 1

    return counters
