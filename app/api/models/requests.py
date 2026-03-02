"""Request and response models for API endpoints"""
from pydantic import BaseModel
from typing import Dict, Any, List


class QueryRequest(BaseModel):
    """Request for RAG query"""
    query: str
    topK: int = 5


class QueryResponse(BaseModel):
    """Response from RAG query"""
    response: str
    sources: List[Dict[str, Any]] = []
