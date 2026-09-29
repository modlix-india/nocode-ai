"""adzump_flows - universal per-flow session state.

Any flow persists its accumulated `data` and resumes from the latest row.
Entities the flow produces live in their own tables; flow POSITION is never
stored (the journey engine recomputes it from the data).
"""

from __future__ import annotations

import json

from app.db.connection import execute_query


async def upsert_flow(
    client_code: str, product_id: int, session_id: str, flow: str,
    status: str, data: dict, user_id: int = 0,
) -> None:
    """Insert or update the flow-state row for (client, product, chat session,
    flow). Several runs per product are allowed; resume reads the most recently
    updated one (latest_flow)."""
    await execute_query(
        """
        INSERT INTO adzump_flows
            (client_code, product_id, session_id, flow, status,
             data, created_by, updated_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s) AS new
        ON DUPLICATE KEY UPDATE
            status=new.status, data=new.data, updated_by=new.updated_by
        """,
        (client_code, product_id, session_id or "", flow,
         status or "draft", json.dumps(data), user_id, user_id),
    )


async def latest_flow(
    client_code: str, product_id: int | None, flow: str,
) -> dict | None:
    """The most recently updated state for (product, flow) - what resume
    hydrates from - or None when no run has been persisted yet."""
    if product_id is None:
        return None
    rows = await execute_query(
        "SELECT data FROM adzump_flows "
        "WHERE client_code=%s AND product_id=%s AND flow=%s "
        "ORDER BY updated_at DESC, id DESC LIMIT 1",
        (client_code, product_id, flow),
    )
    if not rows:
        return None
    raw = rows[0]["data"]
    return raw if isinstance(raw, dict) else json.loads(raw)
