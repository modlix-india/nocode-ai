"""RAG query engine setup"""
import logging
from llama_index.llms.anthropic import Anthropic
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.core import VectorStoreIndex, Settings as LlamaSettings
import chromadb
from app.config import settings
from app.rag.embeddings import get_embedding_model

logger = logging.getLogger(__name__)

# Global instances
_query_engine = None
_index = None
_initialized = False


async def initialize_rag_engine():
    """Initialize the RAG engine with Claude and ChromaDB"""
    global _query_engine, _index, _initialized
    
    if _initialized:
        logger.info("RAG engine already initialized")
        return
    
    logger.info("Initializing RAG engine...")
    
    # Configure LLM (Claude only)
    if settings.ANTHROPIC_API_KEY:
        LlamaSettings.llm = Anthropic(
            model=settings.CLAUDE_MODEL,
            api_key=settings.ANTHROPIC_API_KEY,
            max_tokens=8192
        )
        logger.info(f"Configured Claude LLM: {settings.CLAUDE_MODEL}")
    else:
        logger.warning("ANTHROPIC_API_KEY not set - LLM calls will fail")
    
    # Configure embedding model (switchable)
    try:
        LlamaSettings.embed_model = get_embedding_model()
    except Exception as e:
        logger.error(f"Failed to initialize embedding model: {e}")
        raise
    
    # Load ChromaDB
    try:
        chroma_client = chromadb.PersistentClient(
            path=settings.CHROMA_PERSIST_DIR
        )
        collection = chroma_client.get_or_create_collection("nocode_docs")
        vector_store = ChromaVectorStore(chroma_collection=collection)
        
        # Create index from vector store
        _index = VectorStoreIndex.from_vector_store(
            vector_store,
            embed_model=LlamaSettings.embed_model
        )
        
        # Create query engine
        _query_engine = _index.as_query_engine(
            similarity_top_k=10,
            response_mode="compact"
        )
        
        logger.info(f"RAG engine initialized with ChromaDB at {settings.CHROMA_PERSIST_DIR}")
        _initialized = True
        
    except Exception as e:
        logger.error(f"Failed to initialize ChromaDB: {e}")
        # Don't fail - index might need to be created first
        logger.warning("Run scripts/ingest.py to create the index")


def get_query_engine():
    """Get the query engine instance"""
    return _query_engine


def get_index():
    """Get the vector index instance"""
    return _index


def is_initialized() -> bool:
    """Check if RAG engine is initialized"""
    return _initialized

