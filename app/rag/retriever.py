"""Context retrieval for agents"""
import logging
from typing import Optional, List
from llama_index.core.retrievers import VectorIndexRetriever
from app.rag.engine import get_index

logger = logging.getLogger(__name__)


async def retrieve_context(
    query: str,
    filter_docs: Optional[List[str]] = None,
    top_k: int = 5
) -> str:
    """
    Retrieve relevant context for a query.
    
    Args:
        query: The search query
        filter_docs: Optional list of document filenames to filter by
        top_k: Number of results to retrieve
    
    Returns:
        Formatted context string for use in prompts
    """
    index = get_index()
    if index is None:
        logger.warning("Index not initialized - returning empty context")
        return ""
    
    try:
        retriever = VectorIndexRetriever(
            index=index,
            similarity_top_k=top_k
        )
        
        nodes = retriever.retrieve(query)
        
        # Filter by document names if specified
        if filter_docs:
            filtered_nodes = []
            for node in nodes:
                filename = node.node.metadata.get("filename", "")
                if any(doc in filename for doc in filter_docs):
                    filtered_nodes.append(node)
            nodes = filtered_nodes if filtered_nodes else nodes[:top_k // 2]
        
        # Format context
        context_parts = []
        for i, node in enumerate(nodes, 1):
            metadata = node.node.metadata
            source = metadata.get("filename", "unknown")
            doc_type = metadata.get("type", "unknown")
            score = f"{node.score:.3f}" if node.score else "N/A"
            
            context_parts.append(
                f"### Source {i}: {source} (type: {doc_type}, relevance: {score})\n"
                f"{node.node.text}\n"
            )
        
        return "\n---\n".join(context_parts)
        
    except Exception as e:
        logger.error(f"Error retrieving context: {e}")
        return ""


async def retrieve_examples(
    query: str,
    definition_type: str,
    top_k: int = 3
) -> str:
    """
    Retrieve example definitions of a specific type.
    
    Args:
        query: The search query
        definition_type: Type of definition (page, function, schema, etc.)
        top_k: Number of examples to retrieve
    """
    index = get_index()
    if index is None:
        return ""
    
    try:
        retriever = VectorIndexRetriever(
            index=index,
            similarity_top_k=top_k * 2  # Retrieve more, then filter
        )
        
        nodes = retriever.retrieve(query)
        
        # Filter by definition type
        example_nodes = [
            node for node in nodes
            if node.node.metadata.get("type") == "example"
            and node.node.metadata.get("definition_type") == definition_type
        ][:top_k]
        
        if not example_nodes:
            return ""
        
        # Format examples
        examples = []
        for node in example_nodes:
            metadata = node.node.metadata
            app_name = metadata.get("app_name", "unknown")
            filename = metadata.get("filename", "unknown")
            
            examples.append(
                f"### Example from {app_name}/{filename}\n"
                f"{node.node.text}\n"
            )
        
        return "\n---\n".join(examples)
        
    except Exception as e:
        logger.error(f"Error retrieving examples: {e}")
        return ""

