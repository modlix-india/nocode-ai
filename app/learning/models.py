"""Pydantic models for the learning loop subsystem."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional, List

from pydantic import BaseModel, Field


class FeedbackType(str, Enum):
    RATING = "RATING"
    CORRECTION = "CORRECTION"
    RETRY = "RETRY"
    UNDO = "UNDO"
    ABANDONMENT = "ABANDONMENT"


class KnowledgeType(str, Enum):
    PATTERN = "PATTERN"
    PITFALL = "PITFALL"
    EXAMPLE = "EXAMPLE"
    LESSON = "LESSON"


class KnowledgeStatus(str, Enum):
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    PENDING_REVIEW = "PENDING_REVIEW"


class FeedbackCreate(BaseModel):
    """Request body for submitting feedback."""

    session_id: str
    turn_number: int
    rating: int = Field(..., ge=-1, le=1)
    feedback_text: Optional[str] = None
    feedback_type: FeedbackType = FeedbackType.RATING


class FeedbackRecord(BaseModel):
    """Full feedback record from DB."""

    id: int
    session_id: str
    turn_number: int
    client_code: str
    user_id: int
    agent_name: Optional[str] = None
    rating: int
    feedback_text: Optional[str] = None
    feedback_type: FeedbackType
    user_instruction: Optional[str] = None
    assistant_summary: Optional[str] = None
    tool_calls_json: Optional[str] = None
    created_at: Optional[datetime] = None


class SessionScore(BaseModel):
    """Computed session-level outcome score."""

    session_id: str
    success_score: Optional[float] = None
    user_satisfaction: Optional[float] = None
    tool_error_rate: Optional[float] = None
    turn_count: int = 0
    tool_call_count: int = 0
    retry_count: int = 0
    undo_count: int = 0
    abandoned: bool = False
    total_tokens: int = 0
    total_latency_ms: int = 0


class KnowledgeEntry(BaseModel):
    """A knowledge base entry for prompt injection."""

    id: int
    knowledge_type: KnowledgeType
    agent_name: str
    category: Optional[str] = None
    title: str
    content: str
    tool_sequence_json: Optional[str] = None
    relevance_score: float = 1.0
    use_count: int = 0
    status: KnowledgeStatus = KnowledgeStatus.ACTIVE


class PromptPatch(BaseModel):
    """A text patch to inject into the system prompt."""

    source: str  # "knowledge", "pitfall", "example", "experiment"
    content: str
    priority: float = 1.0
    token_estimate: int = 0


class AnalyticsSummary(BaseModel):
    """Aggregate analytics for dashboards."""

    period: str
    total_sessions: int = 0
    avg_success_score: Optional[float] = None
    avg_user_satisfaction: Optional[float] = None
    avg_tool_error_rate: Optional[float] = None
    top_failing_tools: List[dict] = []
    top_error_patterns: List[dict] = []
    sessions_with_feedback: int = 0
    positive_feedback_pct: Optional[float] = None
