"""
ChromaDB Vector Store for Pattern Search

This module provides semantic search over extracted patterns using
ChromaDB with embeddings for similarity matching.

Features:
- Vector similarity search for patterns
- Filtering by pattern type, category, quality
- Hybrid search (keyword + semantic)
- Pattern caching for performance
"""

import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass
import hashlib

try:
    import chromadb
    from chromadb.config import Settings
    from chromadb.utils import embedding_functions
    CHROMADB_AVAILABLE = True
except ImportError:
    CHROMADB_AVAILABLE = False
    print("ChromaDB not installed. Run: pip install chromadb")

logger = logging.getLogger(__name__)


@dataclass
class PatternSearchResult:
    """A search result with pattern and relevance score"""
    id: str
    type: str
    name: str
    description: str
    tags: List[str]
    category: str
    quality_score: float
    relevance_score: float
    definition: Dict[str, Any]
    source: str

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "description": self.description,
            "tags": self.tags,
            "category": self.category,
            "quality_score": self.quality_score,
            "relevance_score": self.relevance_score,
            "definition": self.definition,
            "source": self.source
        }


class PatternVectorStore:
    """
    ChromaDB-backed vector store for semantic pattern search.

    Usage:
        store = PatternVectorStore("./extracted_patterns")
        store.index_patterns()  # One-time indexing

        results = store.search("login form with email and password")
        results = store.search("calculator", pattern_type="calculator_pattern")
    """

    # Collection names for different pattern types
    COLLECTIONS = {
        "all": "all_patterns",
        "event_function": "event_patterns",
        "component_tree": "component_patterns",
        "form_pattern": "form_patterns",
        "calculator_pattern": "calculator_patterns",
        "navigation_pattern": "navigation_patterns",
        "modal_pattern": "modal_patterns",
        "list_repeater_pattern": "list_patterns",
        "auth_pattern": "auth_patterns",
        "data_fetch_pattern": "data_fetch_patterns",
        "layout_structure": "layout_patterns",
        "style_theme": "style_patterns",
    }

    def __init__(
        self,
        patterns_dir: str,
        persist_dir: Optional[str] = None,
        embedding_model: str = "all-MiniLM-L6-v2"
    ):
        """
        Initialize the vector store.

        Args:
            patterns_dir: Directory containing extracted patterns
            persist_dir: Directory to persist ChromaDB (default: patterns_dir/chroma)
            embedding_model: Sentence transformer model for embeddings
        """
        if not CHROMADB_AVAILABLE:
            raise ImportError("ChromaDB is required. Install with: pip install chromadb")

        self.patterns_dir = Path(patterns_dir)
        self.persist_dir = persist_dir or str(self.patterns_dir / "chroma")

        # Initialize ChromaDB
        self.client = chromadb.PersistentClient(
            path=self.persist_dir,
            settings=Settings(anonymized_telemetry=False)
        )

        # Use sentence transformers for embeddings
        self.embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=embedding_model
        )

        # Pattern cache
        self.pattern_cache: Dict[str, Dict] = {}

        # Initialize collections
        self.collections: Dict[str, Any] = {}
        self._init_collections()

    def _init_collections(self):
        """Initialize or get existing collections"""
        for key, name in self.COLLECTIONS.items():
            try:
                self.collections[key] = self.client.get_or_create_collection(
                    name=name,
                    embedding_function=self.embedding_fn,
                    metadata={"hnsw:space": "cosine"}
                )
            except Exception as e:
                logger.error(f"Failed to create collection {name}: {e}")

    def index_patterns(self, force_reindex: bool = False):
        """
        Index all patterns from the patterns directory.

        Args:
            force_reindex: If True, delete existing data and reindex
        """
        if force_reindex:
            for collection in self.collections.values():
                # Clear collection
                try:
                    ids = collection.get()["ids"]
                    if ids:
                        collection.delete(ids=ids)
                except Exception as e:
                    logger.warning(f"Could not clear collection: {e}")

        # Load and index patterns by type
        pattern_files = list(self.patterns_dir.glob("*_patterns.json"))

        for pattern_file in pattern_files:
            try:
                with open(pattern_file) as f:
                    patterns = json.load(f)

                if not patterns:
                    continue

                # Determine pattern type from filename
                type_name = pattern_file.stem.replace("_patterns", "")

                # Index to type-specific collection
                if type_name in self.collections:
                    self._index_to_collection(patterns, self.collections[type_name])

                # Also index to "all" collection
                self._index_to_collection(patterns, self.collections["all"])

                logger.info(f"Indexed {len(patterns)} patterns from {pattern_file.name}")

            except Exception as e:
                logger.error(f"Failed to index {pattern_file}: {e}")

    def _index_to_collection(self, patterns: List[Dict], collection):
        """Index patterns to a specific collection"""
        ids = []
        documents = []
        metadatas = []

        for pattern in patterns:
            pattern_id = pattern.get("id", "")

            # Skip if already indexed
            try:
                existing = collection.get(ids=[pattern_id])
                if existing["ids"]:
                    continue
            except:
                pass

            # Create searchable document text
            doc_text = self._create_document_text(pattern)

            # Create metadata for filtering
            metadata = {
                "type": pattern.get("type", ""),
                "category": pattern.get("semantic_category", "other"),
                "quality_score": pattern.get("quality_score", 0.5),
                "name": pattern.get("name", "")[:100],
                "tags": ",".join(pattern.get("semantic_tags", [])[:10]),
                "source_page": pattern.get("source_page", ""),
                "source_app": pattern.get("source_app", ""),
                "component_count": pattern.get("component_count", 0),
                "event_step_count": pattern.get("event_step_count", 0),
            }

            ids.append(pattern_id)
            documents.append(doc_text)
            metadatas.append(metadata)

            # Cache full pattern
            self.pattern_cache[pattern_id] = pattern

        if ids:
            # Batch insert
            batch_size = 100
            for i in range(0, len(ids), batch_size):
                batch_ids = ids[i:i + batch_size]
                batch_docs = documents[i:i + batch_size]
                batch_meta = metadatas[i:i + batch_size]

                try:
                    collection.add(
                        ids=batch_ids,
                        documents=batch_docs,
                        metadatas=batch_meta
                    )
                except Exception as e:
                    logger.error(f"Failed to add batch: {e}")

    def _create_document_text(self, pattern: Dict) -> str:
        """Create searchable document text from pattern"""
        parts = [
            pattern.get("name", ""),
            pattern.get("description", ""),
            " ".join(pattern.get("semantic_tags", [])),
            pattern.get("semantic_category", ""),
        ]

        # Add binding paths as keywords
        if pattern.get("required_store_paths"):
            parts.append(" ".join(pattern["required_store_paths"][:5]))

        # Add event info for event patterns
        definition = pattern.get("definition", {})
        if "steps" in definition:
            steps = definition["steps"]
            func_names = [s.get("name", "") for s in steps.values()]
            parts.append(" ".join(func_names))

        return " ".join(filter(None, parts))

    def search(
        self,
        query: str,
        pattern_type: Optional[str] = None,
        category: Optional[str] = None,
        min_quality: float = 0.0,
        tags: Optional[List[str]] = None,
        n_results: int = 10
    ) -> List[PatternSearchResult]:
        """
        Search for patterns matching the query.

        Args:
            query: Natural language search query
            pattern_type: Filter by pattern type (e.g., "event_function", "form_pattern")
            category: Filter by semantic category (e.g., "login", "calculator")
            min_quality: Minimum quality score (0-1)
            tags: Filter by tags (patterns must have at least one matching tag)
            n_results: Maximum number of results

        Returns:
            List of PatternSearchResult sorted by relevance
        """
        # Select collection
        if pattern_type and pattern_type in self.collections:
            collection = self.collections[pattern_type]
        else:
            collection = self.collections["all"]

        # Build where filter
        where_filter = {}

        if category:
            where_filter["category"] = category

        if min_quality > 0:
            where_filter["quality_score"] = {"$gte": min_quality}

        # Perform search
        try:
            results = collection.query(
                query_texts=[query],
                n_results=n_results * 2,  # Get more to filter
                where=where_filter if where_filter else None,
                include=["documents", "metadatas", "distances"]
            )
        except Exception as e:
            logger.error(f"Search failed: {e}")
            return []

        # Process results
        search_results = []

        if results and results["ids"] and results["ids"][0]:
            ids = results["ids"][0]
            distances = results["distances"][0] if results["distances"] else [1.0] * len(ids)
            metadatas = results["metadatas"][0] if results["metadatas"] else [{}] * len(ids)

            for i, pattern_id in enumerate(ids):
                metadata = metadatas[i]
                distance = distances[i]

                # Filter by tags if specified
                if tags:
                    pattern_tags = metadata.get("tags", "").split(",")
                    if not any(t in pattern_tags for t in tags):
                        continue

                # Get full pattern from cache or load
                full_pattern = self._get_full_pattern(pattern_id)

                if full_pattern:
                    # Convert distance to similarity score (cosine distance)
                    relevance = 1 - distance

                    result = PatternSearchResult(
                        id=pattern_id,
                        type=metadata.get("type", ""),
                        name=metadata.get("name", ""),
                        description=full_pattern.get("description", ""),
                        tags=full_pattern.get("semantic_tags", []),
                        category=metadata.get("category", ""),
                        quality_score=metadata.get("quality_score", 0.5),
                        relevance_score=relevance,
                        definition=full_pattern.get("definition", {}),
                        source=f"{metadata.get('source_app', '')}/{metadata.get('source_page', '')}"
                    )
                    search_results.append(result)

        # Sort by combined score (relevance * quality)
        search_results.sort(
            key=lambda x: x.relevance_score * (0.5 + 0.5 * x.quality_score),
            reverse=True
        )

        return search_results[:n_results]

    def search_similar(
        self,
        pattern_id: str,
        n_results: int = 5
    ) -> List[PatternSearchResult]:
        """
        Find patterns similar to a given pattern.

        Args:
            pattern_id: ID of the reference pattern
            n_results: Number of results

        Returns:
            List of similar patterns
        """
        # Get the pattern
        pattern = self._get_full_pattern(pattern_id)
        if not pattern:
            return []

        # Create search text from pattern
        search_text = self._create_document_text(pattern)

        # Search with same type
        return self.search(
            search_text,
            pattern_type=pattern.get("type"),
            n_results=n_results + 1  # +1 because it might include itself
        )

    def get_by_id(self, pattern_id: str) -> Optional[Dict]:
        """Get a specific pattern by ID"""
        return self._get_full_pattern(pattern_id)

    def get_by_category(
        self,
        category: str,
        n_results: int = 20
    ) -> List[PatternSearchResult]:
        """Get top patterns for a category"""
        return self.search(
            category,
            category=category,
            min_quality=0.6,
            n_results=n_results
        )

    def get_by_tags(
        self,
        tags: List[str],
        pattern_type: Optional[str] = None,
        n_results: int = 10
    ) -> List[PatternSearchResult]:
        """Get patterns matching specific tags"""
        return self.search(
            " ".join(tags),
            pattern_type=pattern_type,
            tags=tags,
            n_results=n_results
        )

    def _get_full_pattern(self, pattern_id: str) -> Optional[Dict]:
        """Get full pattern from cache or load from files"""
        if pattern_id in self.pattern_cache:
            return self.pattern_cache[pattern_id]

        # Try to find in pattern files
        for pattern_file in self.patterns_dir.glob("*_patterns.json"):
            try:
                with open(pattern_file) as f:
                    patterns = json.load(f)

                for pattern in patterns:
                    if pattern.get("id") == pattern_id:
                        self.pattern_cache[pattern_id] = pattern
                        return pattern
            except:
                continue

        return None

    def get_statistics(self) -> Dict[str, Any]:
        """Get statistics about indexed patterns"""
        stats = {
            "collections": {},
            "total_patterns": 0
        }

        for key, collection in self.collections.items():
            try:
                count = collection.count()
                stats["collections"][key] = count
                if key == "all":
                    stats["total_patterns"] = count
            except:
                stats["collections"][key] = 0

        return stats


class HybridPatternSearch:
    """
    Hybrid search combining vector similarity with keyword matching.

    This provides better results by combining:
    - Semantic similarity (understands meaning)
    - Keyword matching (exact term matches)
    - Quality scoring (prefers higher quality patterns)
    """

    def __init__(self, vector_store: PatternVectorStore):
        self.vector_store = vector_store

    def search(
        self,
        query: str,
        pattern_type: Optional[str] = None,
        category: Optional[str] = None,
        min_quality: float = 0.0,
        n_results: int = 10,
        keyword_boost: float = 0.3
    ) -> List[PatternSearchResult]:
        """
        Perform hybrid search with keyword boosting.

        Args:
            query: Search query
            pattern_type: Filter by type
            category: Filter by category
            min_quality: Minimum quality
            n_results: Number of results
            keyword_boost: How much to boost keyword matches (0-1)

        Returns:
            Combined and reranked results
        """
        # Get semantic results
        semantic_results = self.vector_store.search(
            query,
            pattern_type=pattern_type,
            category=category,
            min_quality=min_quality,
            n_results=n_results * 2
        )

        # Extract query keywords
        keywords = self._extract_keywords(query)

        # Rerank with keyword boost
        for result in semantic_results:
            keyword_score = self._calculate_keyword_score(result, keywords)
            # Combine scores
            result.relevance_score = (
                result.relevance_score * (1 - keyword_boost) +
                keyword_score * keyword_boost
            )

        # Re-sort
        semantic_results.sort(
            key=lambda x: x.relevance_score * (0.5 + 0.5 * x.quality_score),
            reverse=True
        )

        return semantic_results[:n_results]

    def _extract_keywords(self, query: str) -> List[str]:
        """Extract meaningful keywords from query"""
        # Simple keyword extraction (could be enhanced with NLP)
        stopwords = {
            "a", "an", "the", "is", "are", "was", "were", "be", "been",
            "being", "have", "has", "had", "do", "does", "did", "will",
            "would", "could", "should", "may", "might", "must", "shall",
            "can", "need", "dare", "ought", "used", "to", "of", "in",
            "for", "on", "with", "at", "by", "from", "as", "into",
            "through", "during", "before", "after", "above", "below",
            "between", "under", "again", "further", "then", "once",
            "here", "there", "when", "where", "why", "how", "all",
            "each", "few", "more", "most", "other", "some", "such",
            "no", "nor", "not", "only", "own", "same", "so", "than",
            "too", "very", "just", "and", "but", "if", "or", "because",
            "until", "while", "create", "make", "build", "add", "want",
            "need", "page", "component", "button"
        }

        words = query.lower().split()
        keywords = [w for w in words if w not in stopwords and len(w) > 2]

        return keywords

    def _calculate_keyword_score(
        self,
        result: PatternSearchResult,
        keywords: List[str]
    ) -> float:
        """Calculate keyword match score"""
        if not keywords:
            return 0.0

        # Text to search in
        search_text = (
            f"{result.name} {result.description} "
            f"{' '.join(result.tags)} {result.category}"
        ).lower()

        # Count matches
        matches = sum(1 for kw in keywords if kw in search_text)

        return matches / len(keywords)


# Factory function
def create_pattern_search(
    patterns_dir: str,
    persist_dir: Optional[str] = None,
    use_hybrid: bool = True
) -> HybridPatternSearch:
    """
    Create a pattern search instance.

    Args:
        patterns_dir: Directory with extracted patterns
        persist_dir: ChromaDB persistence directory
        use_hybrid: Whether to use hybrid search

    Returns:
        HybridPatternSearch instance
    """
    vector_store = PatternVectorStore(patterns_dir, persist_dir)
    vector_store.index_patterns()

    if use_hybrid:
        return HybridPatternSearch(vector_store)
    else:
        return vector_store


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python pattern_vectorstore.py <patterns_dir> [query]")
        sys.exit(1)

    patterns_dir = sys.argv[1]
    query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "login form"

    print(f"Initializing vector store from {patterns_dir}...")
    search = create_pattern_search(patterns_dir)

    print(f"\nSearching for: '{query}'")
    results = search.search(query, n_results=5)

    print(f"\nFound {len(results)} results:\n")
    for i, result in enumerate(results, 1):
        print(f"{i}. [{result.type}] {result.name}")
        print(f"   Category: {result.category}")
        print(f"   Tags: {', '.join(result.tags[:5])}")
        print(f"   Quality: {result.quality_score:.2f}, Relevance: {result.relevance_score:.2f}")
        print(f"   Source: {result.source}")
        print(f"   {result.description[:100]}...")
        print()
