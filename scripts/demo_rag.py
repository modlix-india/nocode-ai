#!/usr/bin/env python
"""
Demo script to understand how RAG works in the AI service.

Shows:
1. What documents are in the ChromaDB vector store
2. How a query is converted to embeddings
3. What context is retrieved based on similarity
4. What gets sent to the LLM

Usage:
    python scripts/demo_rag.py "create a login form with validation"
"""
import sys
import os
from pathlib import Path

# Disable telemetry
os.environ["ANONYMIZED_TELEMETRY"] = "False"

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import chromadb
from llama_index.core import VectorStoreIndex, Settings as LlamaSettings
from llama_index.core.retrievers import VectorIndexRetriever
from llama_index.vector_stores.chroma import ChromaVectorStore

from app.config import settings
from app.rag.embeddings import get_embedding_model


def main():
    query = sys.argv[1] if len(sys.argv) > 1 else "create a button with click event"
    
    print("=" * 80)
    print("RAG SYSTEM DEMO")
    print("=" * 80)
    
    # 1. Show what's in ChromaDB
    print("\n" + "=" * 80)
    print("STEP 1: WHAT'S IN THE VECTOR DATABASE")
    print("=" * 80)
    
    chroma_client = chromadb.PersistentClient(path=settings.CHROMA_PERSIST_DIR)
    collection = chroma_client.get_or_create_collection("nocode_docs")
    
    # Get collection stats
    count = collection.count()
    print(f"\n📊 Total documents in ChromaDB: {count}")
    
    # Sample some documents
    sample = collection.peek(limit=10)
    
    print(f"\n📄 Sample of ingested documents:")
    print("-" * 60)
    
    doc_types = {}
    for i, (doc_id, metadata) in enumerate(zip(sample['ids'], sample['metadatas'])):
        doc_type = metadata.get('type', 'unknown')
        filename = metadata.get('filename', 'unknown')
        source = metadata.get('source', 'unknown')
        
        doc_types[doc_type] = doc_types.get(doc_type, 0) + 1
        
        if i < 5:  # Show first 5
            print(f"  [{doc_type:12}] {filename}")
            print(f"               → {source[:70]}...")
    
    print(f"\n📊 Document types breakdown (from sample):")
    for dtype, dcount in doc_types.items():
        print(f"  - {dtype}: {dcount}")
    
    # 2. Show how query gets embedded
    print("\n" + "=" * 80)
    print("STEP 2: QUERY EMBEDDING")
    print("=" * 80)
    
    print(f"\n🔍 Query: \"{query}\"")
    
    embed_model = get_embedding_model()
    query_embedding = embed_model.get_query_embedding(query)
    
    print(f"\n📐 Embedding dimensions: {len(query_embedding)}")
    print(f"📐 First 10 values: {query_embedding[:10]}")
    print(f"📐 Embedding norm: {sum(x**2 for x in query_embedding) ** 0.5:.4f}")
    
    # 3. Show similarity search results
    print("\n" + "=" * 80)
    print("STEP 3: SIMILARITY SEARCH (What ChromaDB Returns)")
    print("=" * 80)
    
    # Direct ChromaDB query to see raw results
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=10,
        include=["documents", "metadatas", "distances"]
    )
    
    print(f"\n🎯 Top 10 most similar documents:")
    print("-" * 60)
    
    for i, (doc_id, metadata, distance, document) in enumerate(zip(
        results['ids'][0],
        results['metadatas'][0],
        results['distances'][0],
        results['documents'][0]
    )):
        doc_type = metadata.get('type', 'unknown')
        filename = metadata.get('filename', 'unknown')
        def_type = metadata.get('definition_type', '-')
        similarity = 1 - distance  # Convert distance to similarity
        
        print(f"\n  [{i+1}] Similarity: {similarity:.3f}")
        print(f"      Type: {doc_type} | Definition: {def_type} | File: {filename}")
        print(f"      Preview: {document[:150]}...")
    
    # 4. Show what the retriever returns (formatted context)
    print("\n" + "=" * 80)
    print("STEP 4: FORMATTED CONTEXT (What Gets Sent to LLM)")
    print("=" * 80)
    
    # Setup LlamaIndex for retrieval
    LlamaSettings.embed_model = embed_model
    vector_store = ChromaVectorStore(chroma_collection=collection)
    index = VectorStoreIndex.from_vector_store(vector_store, embed_model=embed_model)
    
    retriever = VectorIndexRetriever(
        index=index,
        similarity_top_k=5  # What agents use
    )
    
    nodes = retriever.retrieve(query)
    
    print(f"\n📝 Retrieved {len(nodes)} nodes for context")
    
    # Format like the actual retriever does
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
    
    formatted_context = "\n---\n".join(context_parts)
    
    print("-" * 60)
    print("\n🤖 THIS IS WHAT GETS SENT TO THE LLM:")
    print("-" * 60)
    print(formatted_context[:3000])
    if len(formatted_context) > 3000:
        print(f"\n... (truncated, total {len(formatted_context)} chars)")
    
    # 5. Show agent filtering
    print("\n" + "=" * 80)
    print("STEP 5: AGENT-SPECIFIC FILTERING")
    print("=" * 80)
    
    # Example: what the Component agent looks for
    component_docs = [
        "03-component-system.md",
        "22-component-reference.md",
        "02-application-and-page-definitions.md"
    ]
    
    print(f"\n🔧 Component Agent filters for: {component_docs}")
    
    filtered_nodes = []
    for node in nodes:
        filename = node.node.metadata.get("filename", "")
        if any(doc in filename for doc in component_docs):
            filtered_nodes.append(node)
    
    print(f"   → Found {len(filtered_nodes)} matching nodes from {len(nodes)} retrieved")
    
    if filtered_nodes:
        print(f"\n   Filtered sources:")
        for node in filtered_nodes:
            print(f"   - {node.node.metadata.get('filename')} (score: {node.score:.3f})")
    
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"""
1. INGESTION: Markdown docs + JSON examples → chunked → embedded → stored in ChromaDB
   
2. QUERY: User request "{query}"
   → Converted to {len(query_embedding)}-dim embedding
   
3. SEARCH: Find {len(nodes)} most similar chunks by vector distance
   
4. FILTER: Each agent filters for relevant doc types:
   - Layout Agent: layout docs, grid examples
   - Component Agent: component reference, property docs
   - Events Agent: event system, function examples
   - Styles Agent: style system, CSS properties
   
5. SEND TO LLM: Formatted context (~{len(formatted_context)} chars) + user request
   → LLM generates page definition based on examples and docs
""")


if __name__ == "__main__":
    main()

