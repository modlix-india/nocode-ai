"""Embedding model - uses FastEmbed (lightweight ONNX-based)"""
import logging
import os
from llama_index.embeddings.fastembed import FastEmbedEmbedding
from app.config import settings

logger = logging.getLogger(__name__)


def get_embedding_model(parallel: bool = False, num_threads: int = None):
    """
    Get the FastEmbed embedding model.
    
    Uses ONNX runtime instead of PyTorch - much lighter:
    - ~200MB RAM vs 2GB+ for sentence-transformers
    - Faster inference
    - No CUDA/GPU dependencies
    
    Args:
        parallel: If True, enable parallel processing for batch embedding
        num_threads: Number of threads to use (default: all CPU cores)
    
    Default model: BAAI/bge-small-en-v1.5
    """
    model_name = settings.LOCAL_EMBEDDING_MODEL
    
    # Get number of CPU cores for parallel processing
    if num_threads is None:
        num_threads = os.cpu_count() or 4
    
    if parallel:
        logger.info(f"Using FastEmbed model: {model_name} (parallel: {num_threads} threads)")
    else:
        logger.info(f"Using FastEmbed model: {model_name}")
    
    # FastEmbedEmbedding uses 'threads' for parallel processing
    return FastEmbedEmbedding(
        model_name=model_name,
        threads=num_threads if parallel else None
    )
