"""Learning loop API endpoints.

Endpoints:
    POST /feedback                  — Submit user feedback for a turn
    GET  /feedback                  — List feedback history
    GET  /analytics/summary         — Aggregate analytics
    GET  /knowledge                 — List knowledge entries (admin)
    GET  /knowledge/{id}            — Full knowledge entry detail
    PATCH /knowledge/{id}           — Update knowledge entry status (admin)
    GET  /tool-errors               — List tool error patterns
    PATCH /tool-errors/{id}         — Resolve/ignore a tool error pattern
    GET  /session-scores            — List session scores
    GET  /session-scores/{session_id} — Score detail for a session
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Request, HTTPException

from app.learning.models import FeedbackCreate

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_db():
    from app.db.connection import is_pool_available
    if not is_pool_available():
        raise HTTPException(status_code=503, detail="Database not available")


def _get_conn():
    from app.db.connection import get_connection
    return get_connection()


async def _authenticate(request: Request):
    """Reuse session auth (lightweight, no target app). Works for any agent."""
    from app.agents.appbuilder.router import _authenticate_session_request
    return await _authenticate_session_request(request)


# ── Feedback ─────────────────────────────────────────────────


async def _get_session_agent_name(session_id: str) -> str:
    """Look up the agent name from the session record."""
    _require_db()
    async with _get_conn() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT AGENT_NAME FROM ai_tracking_sessions WHERE SESSION_ID = %s",
                (session_id,),
            )
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Session not found")
            return row[0] or "appbuilder"


@router.post("/feedback")
async def submit_feedback(request: Request, body: FeedbackCreate):
    """Submit user feedback for a specific turn in a session."""
    auth = await _authenticate(request)

    agent_name = await _get_session_agent_name(body.session_id)

    from app.learning.feedback import get_feedback_collector
    collector = get_feedback_collector()

    feedback_id = await collector.record_explicit_feedback(
        feedback=body,
        client_code=auth.client_code,
        user_id=auth.user_id,
        agent_name=agent_name,
    )

    if feedback_id is None:
        raise HTTPException(status_code=500, detail="Failed to record feedback")

    return {"feedback_id": feedback_id, "agent_name": agent_name, "status": "recorded"}


@router.get("/feedback")
async def list_feedback(
    request: Request,
    agent_name: str = "appbuilder",
    rating: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
):
    """List feedback history. Filter by rating (-1, 0, 1) optionally."""
    await _authenticate(request)
    _require_db()

    where = "WHERE AGENT_NAME = %s"
    params: list = [agent_name]
    if rating is not None:
        where += " AND RATING = %s"
        params.append(rating)

    async with _get_conn() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"""SELECT ID, SESSION_ID, TURN_NUMBER, RATING,
                           FEEDBACK_TYPE, FEEDBACK_TEXT,
                           USER_INSTRUCTION, ASSISTANT_SUMMARY,
                           CREATED_AT
                    FROM ai_learning_feedback
                    {where}
                    ORDER BY CREATED_AT DESC
                    LIMIT %s OFFSET %s""",
                tuple(params + [limit, offset]),
            )
            rows = await cursor.fetchall()

    items = [
        {
            "id": r[0], "session_id": r[1], "turn_number": r[2],
            "rating": r[3], "feedback_type": r[4],
            "feedback_text": r[5],
            "user_instruction": (r[6] or "")[:300],
            "assistant_summary": (r[7] or "")[:300],
            "created_at": str(r[8]),
        }
        for r in rows
    ]
    return {"items": items, "limit": limit, "offset": offset}


# ── Analytics ────────────────────────────────────────────────


@router.get("/analytics/summary")
async def get_analytics_summary(
    request: Request,
    agent_name: str = "appbuilder",
    period: str = "week",
):
    """Get aggregate analytics for the learning loop."""
    await _authenticate(request)

    from app.learning.analytics import get_learning_analytics
    analytics = get_learning_analytics()
    summary = await analytics.get_summary(agent_name=agent_name, period=period)

    if summary is None:
        raise HTTPException(status_code=503, detail="Analytics not available")

    return summary.model_dump()


# ── Knowledge ────────────────────────────────────────────────


@router.get("/knowledge")
async def list_knowledge(
    request: Request,
    agent_name: str = "appbuilder",
    knowledge_type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
):
    """List knowledge entries. Filter by type (PATTERN/PITFALL/EXAMPLE/LESSON) and status."""
    await _authenticate(request)
    _require_db()

    where = "WHERE AGENT_NAME = %s"
    params: list = [agent_name]
    if knowledge_type:
        where += " AND KNOWLEDGE_TYPE = %s"
        params.append(knowledge_type)
    if status:
        where += " AND STATUS = %s"
        params.append(status)

    async with _get_conn() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"""SELECT ID, KNOWLEDGE_TYPE, AGENT_NAME, CATEGORY,
                           TITLE, CONTENT, RELEVANCE_SCORE, USE_COUNT,
                           POSITIVE_FEEDBACK_COUNT, NEGATIVE_FEEDBACK_COUNT,
                           STATUS, CREATED_AT
                    FROM ai_learning_knowledge
                    {where}
                    ORDER BY RELEVANCE_SCORE DESC, CREATED_AT DESC
                    LIMIT %s OFFSET %s""",
                tuple(params + [limit, offset]),
            )
            rows = await cursor.fetchall()

    items = [
        {
            "id": r[0], "type": r[1], "agent": r[2],
            "category": r[3], "title": r[4],
            "content_preview": (r[5] or "")[:200],
            "relevance_score": r[6], "use_count": r[7],
            "positive_feedback": r[8], "negative_feedback": r[9],
            "status": r[10], "created_at": str(r[11]),
        }
        for r in rows
    ]
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/knowledge/{knowledge_id}")
async def get_knowledge_detail(request: Request, knowledge_id: int):
    """Get full knowledge entry including content and tool sequence."""
    await _authenticate(request)
    _require_db()

    async with _get_conn() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """SELECT ID, KNOWLEDGE_TYPE, AGENT_NAME, CATEGORY,
                          TITLE, CONTENT, SOURCE_SESSION_IDS,
                          TOOL_SEQUENCE_JSON, RELEVANCE_SCORE, USE_COUNT,
                          POSITIVE_FEEDBACK_COUNT, NEGATIVE_FEEDBACK_COUNT,
                          STATUS, CREATED_AT, UPDATED_AT
                   FROM ai_learning_knowledge WHERE ID = %s""",
                (knowledge_id,),
            )
            r = await cursor.fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="Knowledge entry not found")

    return {
        "id": r[0], "type": r[1], "agent": r[2], "category": r[3],
        "title": r[4], "content": r[5],
        "source_session_ids": r[6], "tool_sequence_json": r[7],
        "relevance_score": r[8], "use_count": r[9],
        "positive_feedback": r[10], "negative_feedback": r[11],
        "status": r[12],
        "created_at": str(r[13]), "updated_at": str(r[14]),
    }


@router.patch("/knowledge/{knowledge_id}")
async def update_knowledge(
    request: Request,
    knowledge_id: int,
    status: Optional[str] = None,
    relevance_score: Optional[float] = None,
):
    """Update a knowledge entry's status or relevance score.

    Set status to DEPRECATED to discard an entry.
    Set status to ACTIVE to re-enable a deprecated entry.
    Set relevance_score to 0.0 to deprioritize without discarding.
    """
    await _authenticate(request)
    _require_db()

    updates: list[str] = []
    params: list = []
    if status:
        valid = {"ACTIVE", "DEPRECATED", "PENDING_REVIEW"}
        if status not in valid:
            raise HTTPException(status_code=400, detail=f"status must be one of {valid}")
        updates.append("STATUS = %s")
        params.append(status)
    if relevance_score is not None:
        updates.append("RELEVANCE_SCORE = %s")
        params.append(relevance_score)

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    params.append(knowledge_id)

    async with _get_conn() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"UPDATE ai_learning_knowledge SET {', '.join(updates)} WHERE ID = %s",
                tuple(params),
            )
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Knowledge entry not found")

    return {"id": knowledge_id, "status": "updated"}


# ── Tool Errors ──────────────────────────────────────────────


@router.get("/tool-errors")
async def list_tool_errors(
    request: Request,
    agent_name: str = "appbuilder",
    tool_name: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """List tool error patterns. Shows what the agent keeps getting wrong."""
    await _authenticate(request)
    _require_db()

    where = "WHERE AGENT_NAME = %s"
    params: list = [agent_name]
    if tool_name:
        where += " AND TOOL_NAME = %s"
        params.append(tool_name)
    if status:
        where += " AND STATUS = %s"
        params.append(status)

    async with _get_conn() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"""SELECT ID, TOOL_NAME, ERROR_PATTERN, OCCURRENCE_COUNT,
                           EXAMPLE_INPUT_JSON, RESOLUTION, STATUS,
                           LAST_SEEN_AT, CREATED_AT
                    FROM ai_learning_tool_errors
                    {where}
                    ORDER BY OCCURRENCE_COUNT DESC
                    LIMIT %s OFFSET %s""",
                tuple(params + [limit, offset]),
            )
            rows = await cursor.fetchall()

    items = [
        {
            "id": r[0], "tool_name": r[1], "error_pattern": r[2],
            "occurrence_count": r[3],
            "example_input": r[4],
            "resolution": r[5], "status": r[6],
            "last_seen_at": str(r[7]), "created_at": str(r[8]),
        }
        for r in rows
    ]
    return {"items": items, "limit": limit, "offset": offset}


@router.patch("/tool-errors/{error_id}")
async def update_tool_error(
    request: Request,
    error_id: int,
    status: Optional[str] = None,
    resolution: Optional[str] = None,
):
    """Update a tool error pattern.

    Set status to RESOLVED when you've fixed the root cause.
    Set status to IGNORED to suppress it.
    Add a resolution note to document the fix.
    """
    await _authenticate(request)
    _require_db()

    updates: list[str] = []
    params: list = []
    if status:
        valid = {"ACTIVE", "RESOLVED", "IGNORED"}
        if status not in valid:
            raise HTTPException(status_code=400, detail=f"status must be one of {valid}")
        updates.append("STATUS = %s")
        params.append(status)
    if resolution is not None:
        updates.append("RESOLUTION = %s")
        params.append(resolution)

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    params.append(error_id)

    async with _get_conn() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"UPDATE ai_learning_tool_errors SET {', '.join(updates)} WHERE ID = %s",
                tuple(params),
            )
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Tool error not found")

    return {"id": error_id, "status": "updated"}


# ── Session Scores ───────────────────────────────────────────


@router.get("/session-scores")
async def list_session_scores(
    request: Request,
    agent_name: str = "appbuilder",
    min_score: Optional[float] = None,
    max_score: Optional[float] = None,
    limit: int = 50,
    offset: int = 0,
):
    """List session scores. Filter by score range to find best/worst sessions."""
    await _authenticate(request)
    _require_db()

    where = "WHERE AGENT_NAME = %s"
    params: list = [agent_name]
    if min_score is not None:
        where += " AND SUCCESS_SCORE >= %s"
        params.append(min_score)
    if max_score is not None:
        where += " AND SUCCESS_SCORE <= %s"
        params.append(max_score)

    async with _get_conn() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"""SELECT SESSION_ID, SUCCESS_SCORE, USER_SATISFACTION,
                           TOOL_ERROR_RATE, TURN_COUNT, TOOL_CALL_COUNT,
                           RETRY_COUNT, UNDO_COUNT, ABANDONED,
                           TOTAL_TOKENS, COMPUTED_AT
                    FROM ai_learning_session_scores
                    {where}
                    ORDER BY COMPUTED_AT DESC
                    LIMIT %s OFFSET %s""",
                tuple(params + [limit, offset]),
            )
            rows = await cursor.fetchall()

    items = [
        {
            "session_id": r[0], "success_score": r[1],
            "user_satisfaction": r[2], "tool_error_rate": r[3],
            "turn_count": r[4], "tool_call_count": r[5],
            "retry_count": r[6], "undo_count": r[7],
            "abandoned": bool(r[8]), "total_tokens": r[9],
            "computed_at": str(r[10]),
        }
        for r in rows
    ]
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/session-scores/{session_id}")
async def get_session_score_detail(request: Request, session_id: str):
    """Get detailed score breakdown for a specific session."""
    await _authenticate(request)
    _require_db()

    async with _get_conn() as conn:
        async with conn.cursor() as cursor:
            # Score
            await cursor.execute(
                """SELECT SUCCESS_SCORE, USER_SATISFACTION, TOOL_ERROR_RATE,
                          TURN_COUNT, TOOL_CALL_COUNT, RETRY_COUNT,
                          UNDO_COUNT, ABANDONED, TOTAL_TOKENS,
                          TOTAL_LATENCY_MS, SCORE_VERSION, COMPUTED_AT
                   FROM ai_learning_session_scores
                   WHERE SESSION_ID = %s ORDER BY COMPUTED_AT DESC LIMIT 1""",
                (session_id,),
            )
            score_row = await cursor.fetchone()
            if not score_row:
                raise HTTPException(status_code=404, detail="No score for this session")

            # Feedback for this session
            await cursor.execute(
                """SELECT TURN_NUMBER, RATING, FEEDBACK_TYPE, FEEDBACK_TEXT,
                          CREATED_AT
                   FROM ai_learning_feedback
                   WHERE SESSION_ID = %s ORDER BY TURN_NUMBER""",
                (session_id,),
            )
            feedback_rows = await cursor.fetchall()

            # Session title and metadata
            await cursor.execute(
                """SELECT TITLE, AGENT_NAME, APP_CODE, TURN_COUNT, STATUS
                   FROM ai_tracking_sessions WHERE SESSION_ID = %s""",
                (session_id,),
            )
            session_row = await cursor.fetchone()

    feedback = [
        {
            "turn_number": r[0], "rating": r[1],
            "feedback_type": r[2], "feedback_text": r[3],
            "created_at": str(r[4]),
        }
        for r in feedback_rows
    ]

    result = {
        "session_id": session_id,
        "score": {
            "success_score": score_row[0], "user_satisfaction": score_row[1],
            "tool_error_rate": score_row[2], "turn_count": score_row[3],
            "tool_call_count": score_row[4], "retry_count": score_row[5],
            "undo_count": score_row[6], "abandoned": bool(score_row[7]),
            "total_tokens": score_row[8], "total_latency_ms": score_row[9],
            "score_version": score_row[10], "computed_at": str(score_row[11]),
        },
        "feedback": feedback,
    }

    if session_row:
        result["session"] = {
            "title": session_row[0], "agent_name": session_row[1],
            "app_code": session_row[2], "turn_count": session_row[3],
            "status": session_row[4],
        }

    return result
