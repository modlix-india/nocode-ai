#!/usr/bin/env python
"""
Ingestion script for RAG system.

Ingests:
1. AI Context documentation (markdown files)
2. App definition examples (JSON files)
3. Site definition examples (JSON files)

Usage:
    python scripts/ingest.py
"""
import sys
import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import time

# Disable ChromaDB telemetry (suppresses warning messages)
os.environ["ANONYMIZED_TELEMETRY"] = "False"

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from llama_index.core import Document, VectorStoreIndex, Settings as LlamaSettings, StorageContext
from llama_index.core.node_parser import MarkdownNodeParser, SentenceSplitter
from llama_index.vector_stores.chroma import ChromaVectorStore
import chromadb

from app.config import settings
from app.rag.embeddings import get_embedding_model


def load_markdown_docs(path: str) -> list:
    """Load markdown documentation files"""
    documents = []
    base_path = Path(path)
    
    if not base_path.exists():
        print(f"Warning: Path {path} does not exist")
        return documents
    
    for file_path in base_path.rglob("*.md"):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                documents.append(Document(
                    text=content,
                    metadata={
                        "source": str(file_path),
                        "type": "documentation",
                        "filename": file_path.name
                    }
                ))
        except Exception as e:
            print(f"Error loading {file_path}: {e}")
    
    return documents


def load_json_definitions(path: str, def_type: str) -> list:
    """
    Load JSON definition files (Page, Function, Schema, etc.)
    Each JSON file becomes a document with metadata about its type.
    """
    documents = []
    base_path = Path(path)
    
    if not base_path.exists():
        print(f"Warning: Path {path} does not exist")
        return documents
    
    # Iterate through each app/site folder
    for app_folder in base_path.iterdir():
        if not app_folder.is_dir() or app_folder.name.startswith('.'):
            continue
        
        app_name = app_folder.name
        
        # Load each definition type
        for def_folder in ['Page', 'Function', 'Schema', 'Theme', 'Style', 'Filler', 'Application']:
            folder_path = app_folder / def_folder
            if not folder_path.exists():
                continue
            
            for json_file in folder_path.glob("*.json"):
                try:
                    with open(json_file, 'r', encoding='utf-8') as f:
                        content = json.load(f)
                    
                    # Create a descriptive document
                    doc_text = create_example_description(def_folder, json_file.stem, content)
                    
                    documents.append(Document(
                        text=doc_text,
                        metadata={
                            "source": str(json_file),
                            "type": "example",
                            "definition_type": def_folder.lower(),
                            "app_name": app_name,
                            "filename": json_file.stem,
                            "category": def_type
                        }
                    ))
                except Exception as e:
                    print(f"Error loading {json_file}: {e}")
    
    return documents


def create_example_description(def_type: str, name: str, content: dict) -> str:
    """Create a descriptive text for the example that aids retrieval"""
    
    if def_type == "Page":
        components = extract_component_names(content.get("rootComponent", {}))
        events = list(content.get("eventFunctions", {}).keys())
        
        description = f"""## Example Page: {name}

This is an example page definition named "{name}".

### Components Used:
{', '.join(components) if components else 'None'}

### Event Functions:
{', '.join(events) if events else 'None'}

### Full Page Definition:
```json
{json.dumps(content, indent=2)}
```
"""
    elif def_type == "Function":
        steps = list(content.get("steps", {}).keys())
        description = f"""## Example Function: {name}

This is a KIRun function definition named "{name}".

### Function Steps:
{', '.join(steps) if steps else 'None'}

### Full Function Definition:
```json
{json.dumps(content, indent=2)}
```
"""
    elif def_type == "Schema":
        properties = list(content.get("properties", {}).keys())
        description = f"""## Example Schema: {name}

This is a JSON Schema definition named "{name}".

### Properties:
{', '.join(properties) if properties else 'None'}

### Full Schema Definition:
```json
{json.dumps(content, indent=2)}
```
"""
    else:
        description = f"""## Example {def_type}: {name}

### Full Definition:
```json
{json.dumps(content, indent=2)}
```
"""
    
    return description


def extract_component_names(component: dict, names: list = None) -> list:
    """Recursively extract component names from a page structure"""
    if names is None:
        names = []
    
    if isinstance(component, dict):
        if "name" in component:
            names.append(component["name"])
        for child in component.get("children", {}).values():
            extract_component_names(child, names)
    
    return list(set(names))


def main():
    print("=" * 60)
    print("Nocode AI RAG Ingestion (Parallel Mode)")
    print("=" * 60)
    
    num_cpus = os.cpu_count() or 4
    print(f"Using {num_cpus} CPU cores for parallel processing")
    
    all_documents = []
    
    # 1. Load AI Context documentation
    print(f"\n1. Loading documentation from {settings.AICONTEXT_PATH}")
    docs = load_markdown_docs(settings.AICONTEXT_PATH)
    print(f"   Loaded {len(docs)} markdown documents")
    all_documents.extend(docs)
    
    # 2. Load App definition examples
    print(f"\n2. Loading app definitions from {settings.APP_DEFINITIONS_PATH}")
    app_docs = load_json_definitions(settings.APP_DEFINITIONS_PATH, "app")
    print(f"   Loaded {len(app_docs)} app definition examples")
    all_documents.extend(app_docs)
    
    # 3. Load Site definition examples
    print(f"\n3. Loading site definitions from {settings.SITE_DEFINITIONS_PATH}")
    site_docs = load_json_definitions(settings.SITE_DEFINITIONS_PATH, "site")
    print(f"   Loaded {len(site_docs)} site definition examples")
    all_documents.extend(site_docs)
    
    print(f"\nTotal documents: {len(all_documents)}")
    
    if len(all_documents) == 0:
        print("\nNo documents found! Check your paths:")
        print(f"  - AICONTEXT_PATH: {settings.AICONTEXT_PATH}")
        print(f"  - APP_DEFINITIONS_PATH: {settings.APP_DEFINITIONS_PATH}")
        print(f"  - SITE_DEFINITIONS_PATH: {settings.SITE_DEFINITIONS_PATH}")
        return
    
    # Parse into nodes
    print("\n4. Parsing documents into nodes...")
    md_parser = MarkdownNodeParser()
    splitter = SentenceSplitter(chunk_size=1024, chunk_overlap=200)
    
    # Separate markdown and JSON docs for different parsing
    md_docs = [d for d in all_documents if d.metadata.get("type") == "documentation"]
    example_docs = [d for d in all_documents if d.metadata.get("type") == "example"]
    
    md_nodes = md_parser.get_nodes_from_documents(md_docs) if md_docs else []
    example_nodes = splitter.get_nodes_from_documents(example_docs) if example_docs else []
    
    all_nodes = md_nodes + example_nodes
    print(f"   Created {len(all_nodes)} nodes ({len(md_nodes)} from docs, {len(example_nodes)} from examples)")
    
    # Setup ChromaDB
    print(f"\n5. Setting up ChromaDB at {settings.CHROMA_PERSIST_DIR}...")
    Path(settings.CHROMA_PERSIST_DIR).mkdir(parents=True, exist_ok=True)
    chroma_client = chromadb.PersistentClient(path=settings.CHROMA_PERSIST_DIR)
    
    # Clear existing collection
    try:
        chroma_client.delete_collection("nocode_docs")
        print("   Cleared existing collection")
    except:
        pass
    
    collection = chroma_client.create_collection("nocode_docs")
    vector_store = ChromaVectorStore(chroma_collection=collection)
    
    # Create storage context - THIS IS KEY for persistence
    storage_context = StorageContext.from_defaults(vector_store=vector_store)
    
    # Get embedding model with parallel processing enabled
    print(f"\n6. Initializing embedding model ({settings.LOCAL_EMBEDDING_MODEL}) with parallel processing...")
    embed_model = get_embedding_model(parallel=True)
    
    # Set global settings for batch processing
    LlamaSettings.embed_model = embed_model
    LlamaSettings.num_workers = num_cpus
    
    # Create index with storage context - embeddings will persist to ChromaDB
    print(f"\n7. Creating vector index ({len(all_nodes)} nodes)...")
    start_time = time.time()
    
    index = VectorStoreIndex(
        all_nodes,
        storage_context=storage_context,
        embed_model=embed_model,
        show_progress=True,
    )
    
    elapsed = time.time() - start_time
    
    print("\n" + "=" * 60)
    print("Ingestion complete!")
    print(f"Total nodes indexed: {len(all_nodes)}")
    print(f"Time elapsed: {elapsed:.1f} seconds")
    print(f"Speed: {len(all_nodes) / elapsed:.1f} nodes/second")
    print("=" * 60)


if __name__ == "__main__":
    main()
