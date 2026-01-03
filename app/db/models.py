"""
Pydantic models for AI tracking database entities.

These models match the database tables defined in migrations/V1__Initial_AI_Tracking.sql
"""

from datetime import datetime
from typing import Optional, List
from enum import Enum
from pydantic import BaseModel, Field


class SessionStatus(str, Enum):
    """Session status enum matching database ENUM."""
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"


# =============================================================================
# Session Models
# =============================================================================

class AiSessionCreate(BaseModel):
    """Input model for creating a new session."""
    session_id: str = Field(..., max_length=64, description="Unique session ID: clientCode_objectName_shortUUID")
    client_code: str = Field(..., max_length=8, description="Client code")
    client_id: int = Field(..., description="Client ID")
    user_id: int = Field(..., description="User ID")
    object_name: Optional[str] = Field(None, max_length=256, description="Name of the object being tracked (page, function, etc.)")
    agent_name: Optional[str] = Field(None, max_length=64, description="Type of agent (PageAgent, FunctionAgent, etc.)")
    app_code: Optional[str] = Field(None, max_length=64, description="App code (sitezump/appbuilder)")
    context_limit: int = Field(default=184000, description="Context token limit")


class AiSession(BaseModel):
    """Full session model matching database table."""
    id: int
    session_id: str
    client_code: str
    client_id: int
    user_id: int
    object_name: Optional[str] = None
    agent_name: Optional[str] = None
    app_code: Optional[str] = None
    status: SessionStatus = SessionStatus.ACTIVE
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    request_count: int = 0
    turn_count: int = 0
    context_tokens_used: int = 0
    context_limit: int = 184000
    created_by: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_by: Optional[int] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# =============================================================================
# Token Usage Models
# =============================================================================

class AiTokenUsageCreate(BaseModel):
    """Input model for recording token usage."""
    session_id: str = Field(..., max_length=64, description="Session ID")
    request_id: str = Field(..., max_length=64, description="Unique request ID")
    client_code: str = Field(..., max_length=8, description="Client code")
    client_id: int = Field(..., description="Client ID")
    user_id: int = Field(..., description="User ID")
    agent_type: str = Field(..., max_length=32, description="Agent name (Layout, Component, etc.)")
    model: str = Field(..., max_length=64, description="Model name used")
    llm_provider: str = Field(..., max_length=32, description="Provider (anthropic, openai)")
    input_tokens: int = Field(default=0, description="Input tokens consumed")
    output_tokens: int = Field(default=0, description="Output tokens generated")
    cache_read_tokens: int = Field(default=0, description="Tokens read from cache")
    cache_creation_tokens: int = Field(default=0, description="Tokens used for cache creation")
    latency_ms: Optional[int] = Field(None, description="Request latency in milliseconds")
    success: bool = Field(default=True, description="Whether the call succeeded")
    error_message: Optional[str] = Field(None, description="Error message if failed")


class AiTokenUsage(BaseModel):
    """Full token usage model matching database table."""
    id: int
    session_id: str
    request_id: str
    client_code: str
    client_id: int
    user_id: int
    agent_type: str
    model: str
    llm_provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    latency_ms: Optional[int] = None
    success: bool = True
    error_message: Optional[str] = None
    created_by: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_by: Optional[int] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# =============================================================================
# Session History Models (for multi-turn context)
# =============================================================================

class AiSessionHistoryCreate(BaseModel):
    """Input model for adding a conversation turn."""
    session_id: str = Field(..., max_length=64, description="Session ID")
    request_id: str = Field(..., max_length=64, description="Request ID for this turn")
    turn_number: int = Field(..., description="Sequential turn number")
    user_instruction: str = Field(..., description="User's prompt/instruction")
    assistant_summary: Optional[str] = Field(None, description="Summary of what was generated")
    page_snapshot: Optional[str] = Field(None, description="JSON snapshot of page after this turn")
    input_tokens_used: int = Field(default=0, description="Tokens used for context in this turn")


class AiSessionHistory(BaseModel):
    """Full session history model matching database table."""
    id: int
    session_id: str
    request_id: str
    turn_number: int
    user_instruction: str
    assistant_summary: Optional[str] = None
    page_snapshot: Optional[str] = None
    input_tokens_used: int = 0
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# =============================================================================
# Context Usage Model (for API response)
# =============================================================================

class ContextUsage(BaseModel):
    """Context usage information returned in API response."""
    used: int = Field(..., description="Total tokens used in session context")
    limit: int = Field(..., description="Model's context token limit")
    percentage: float = Field(..., description="Percentage of context used")
    turns_in_context: int = Field(..., description="Number of conversation turns in active context")
    warning: Optional[str] = Field(None, description="Warning if approaching/at limit")

    @classmethod
    def from_session(cls, session: AiSession) -> "ContextUsage":
        """Create ContextUsage from session data."""
        percentage = (session.context_tokens_used / session.context_limit * 100) if session.context_limit > 0 else 0

        # Determine warning level
        warning = None
        if percentage >= 95:
            warning = "at_limit"
        elif percentage >= 80:
            warning = "approaching_limit"

        return cls(
            used=session.context_tokens_used,
            limit=session.context_limit,
            percentage=round(percentage, 2),
            turns_in_context=session.turn_count,
            warning=warning
        )


# =============================================================================
# Token Usage Summary (for API response)
# =============================================================================

class TokenUsageSummary(BaseModel):
    """Aggregated token usage for a request."""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    by_agent: dict = Field(default_factory=dict, description="Token usage per agent")

    @classmethod
    def from_usage_records(cls, records: List[AiTokenUsageCreate]) -> "TokenUsageSummary":
        """Create summary from list of usage records."""
        summary = cls()
        by_agent = {}

        for record in records:
            summary.total_input_tokens += record.input_tokens
            summary.total_output_tokens += record.output_tokens
            summary.total_cache_read_tokens += record.cache_read_tokens
            summary.total_cache_creation_tokens += record.cache_creation_tokens

            by_agent[record.agent_type] = {
                "inputTokens": record.input_tokens,
                "outputTokens": record.output_tokens,
                "cacheReadTokens": record.cache_read_tokens,
                "cacheCreationTokens": record.cache_creation_tokens,
                "model": record.model,
                "latencyMs": record.latency_ms
            }

        summary.by_agent = by_agent
        return summary
