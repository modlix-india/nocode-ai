"""Request and response models for API endpoints"""
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
from enum import Enum


class PageAgentMode(str, Enum):
    """Page generation mode"""
    CREATE = "create"      # Generate new page from scratch
    MODIFY = "modify"      # Modify specific aspects of existing page
    ENHANCE = "enhance"    # Add features to existing page


class PageAgentOptions(BaseModel):
    """Options for page generation"""
    mode: PageAgentMode = PageAgentMode.CREATE
    preserveEvents: bool = False    # Keep existing events when modifying
    preserveStyles: bool = False    # Keep existing styles when modifying
    preserveLayout: bool = False    # Keep existing layout when modifying


class PageAgentRequest(BaseModel):
    """Request to generate or modify a page"""
    instruction: str
    existingPage: Optional[Dict[str, Any]] = None
    options: PageAgentOptions = PageAgentOptions()


class AgentLog(BaseModel):
    """Log entry for a single agent's execution"""
    status: str  # "success" | "error"
    reasoning: Optional[str] = None
    errors: List[str] = []


class PageAgentResponse(BaseModel):
    """Response from page generation"""
    success: bool
    page: Dict[str, Any]
    agentLogs: Dict[str, AgentLog]


class QueryRequest(BaseModel):
    """Request for RAG query"""
    query: str
    topK: int = 5


class QueryResponse(BaseModel):
    """Response from RAG query"""
    response: str
    sources: List[Dict[str, Any]] = []

