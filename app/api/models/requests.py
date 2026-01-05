"""Request and response models for API endpoints"""
from pydantic import BaseModel
from typing import Optional, Dict, Any, List

# Import page agent models from canonical source
from app.agents.page_generation.models import (
    PageAgentMode,
    PageAgentOptions,
    PageAgentRequest,
    PageAgentResponse,
    AgentLogEntry,
)

# Re-export for backward compatibility
__all__ = [
    "PageAgentMode",
    "PageAgentOptions",
    "PageAgentRequest",
    "PageAgentResponse",
    "AgentLogEntry",
    "AgentLog",  # Alias
    "QueryRequest",
    "QueryResponse",
]

# Alias for backward compatibility
AgentLog = AgentLogEntry


class QueryRequest(BaseModel):
    """Request for RAG query"""
    query: str
    topK: int = 5


class QueryResponse(BaseModel):
    """Response from RAG query"""
    response: str
    sources: List[Dict[str, Any]] = []
