"""RAG query endpoint"""
from fastapi import APIRouter, Depends, HTTPException
from app.api.models.requests import QueryRequest, QueryResponse
from app.services.security import require_auth
from app.api.models.auth import ContextAuthentication
from app.rag.engine import get_query_engine, is_initialized

router = APIRouter()


@router.post("/", response_model=QueryResponse)
async def query_documentation(
    request: QueryRequest,
    auth: ContextAuthentication = Depends(require_auth)
) -> QueryResponse:
    """
    Query the RAG system for documentation and examples.
    
    This endpoint searches through:
    - AI context documentation
    - App definition examples
    - Site definition examples
    
    **Example:**
    ```
    POST /api/ai/query
    {
      "query": "How do I create a form with validation?",
      "topK": 5
    }
    ```
    """
    if not is_initialized():
        raise HTTPException(
            status_code=503,
            detail="RAG engine not initialized. Run scripts/ingest.py first."
        )
    
    query_engine = get_query_engine()
    if query_engine is None:
        raise HTTPException(
            status_code=503,
            detail="Query engine not available"
        )
    
    try:
        response = query_engine.query(request.query)
        
        # Extract sources
        sources = []
        if hasattr(response, 'source_nodes'):
            for node in response.source_nodes[:request.topK]:
                sources.append({
                    "text": node.node.text[:500] + "..." if len(node.node.text) > 500 else node.node.text,
                    "metadata": node.node.metadata,
                    "score": node.score
                })
        
        return QueryResponse(
            response=str(response),
            sources=sources
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

