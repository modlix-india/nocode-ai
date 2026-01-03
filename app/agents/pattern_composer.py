"""
Pattern Composer Agent - A new approach to page generation

Instead of generating from scratch, this agent:
1. Understands what the user wants (semantic analysis)
2. Retrieves real patterns from the extracted pattern database
3. Adapts/composes patterns to match the request
4. Validates the result

This approach produces valid output because it's based on real,
working page definitions rather than LLM-generated structures.
"""

import json
import logging
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class PatternMatch:
    """A matched pattern from the database"""
    id: str
    type: str
    name: str
    description: str
    tags: List[str]
    score: float
    definition: Dict[str, Any]


@dataclass
class CompositionPlan:
    """A plan for composing patterns into a page"""
    layout_pattern: Optional[PatternMatch]
    component_patterns: List[PatternMatch]
    event_patterns: List[PatternMatch]
    form_patterns: List[PatternMatch]

    # Customizations to apply
    customizations: Dict[str, Any]


class PatternDatabase:
    """
    Manages the extracted patterns and provides semantic search.

    For production, this should be backed by ChromaDB or similar.
    For now, we use a simple in-memory implementation.
    """

    def __init__(self, patterns_dir: str):
        self.patterns_dir = Path(patterns_dir)
        self.patterns: Dict[str, Dict] = {}  # id -> pattern
        self.semantic_index: List[Dict] = []
        self._load_patterns()

    def _load_patterns(self):
        """Load all patterns into memory"""
        # Load semantic index
        index_path = self.patterns_dir / "semantic_index.json"
        if index_path.exists():
            with open(index_path) as f:
                self.semantic_index = json.load(f)

        # Load full patterns by type
        pattern_files = [
            "event_function_patterns.json",
            "component_tree_patterns.json",
            "layout_structure_patterns.json",
            "form_pattern_patterns.json"
        ]

        for filename in pattern_files:
            filepath = self.patterns_dir / filename
            if filepath.exists():
                with open(filepath) as f:
                    patterns = json.load(f)
                    for p in patterns:
                        self.patterns[p["id"]] = p

        logger.info(f"Loaded {len(self.patterns)} patterns, {len(self.semantic_index)} in index")

    def search(
        self,
        query: str,
        pattern_type: Optional[str] = None,
        tags: Optional[List[str]] = None,
        max_results: int = 10
    ) -> List[PatternMatch]:
        """
        Search for patterns matching the query.

        For production, this should use vector similarity search.
        For now, we use keyword matching.
        """
        query_lower = query.lower()
        query_words = set(query_lower.split())

        results = []

        for entry in self.semantic_index:
            # Filter by type
            if pattern_type and entry["type"] != pattern_type:
                continue

            # Filter by tags
            if tags:
                entry_tags = set(entry.get("tags", []))
                if not any(t in entry_tags for t in tags):
                    continue

            # Score by keyword match
            search_text = entry.get("search_text", "").lower()
            entry_tags = set(entry.get("tags", []))

            score = 0.0

            # Tag matches are worth more
            for tag in entry_tags:
                if tag in query_lower:
                    score += 2.0

            # Word matches in search text
            for word in query_words:
                if word in search_text:
                    score += 1.0

            # Exact name match
            if query_lower in entry.get("name", "").lower():
                score += 5.0

            if score > 0:
                # Get full pattern
                full_pattern = self.patterns.get(entry["id"], {})

                results.append(PatternMatch(
                    id=entry["id"],
                    type=entry["type"],
                    name=entry.get("name", ""),
                    description=entry.get("description", ""),
                    tags=entry.get("tags", []),
                    score=score,
                    definition=full_pattern.get("definition", {})
                ))

        # Sort by score
        results.sort(key=lambda x: -x.score)

        return results[:max_results]

    def get_by_id(self, pattern_id: str) -> Optional[Dict]:
        """Get a specific pattern by ID"""
        return self.patterns.get(pattern_id)

    def get_by_tags(
        self,
        tags: List[str],
        pattern_type: Optional[str] = None
    ) -> List[PatternMatch]:
        """Get patterns matching specific tags"""
        return self.search(" ".join(tags), pattern_type=pattern_type, tags=tags)


class SemanticAnalyzer:
    """
    Analyzes user requests to understand semantic intent.

    This uses the LLM to extract structured requirements from natural language.
    """

    ANALYSIS_PROMPT = """Analyze this page generation request and extract structured requirements.

User Request: {request}

Output a JSON object with:
{{
  "page_type": "form|landing|dashboard|list|detail|other",
  "primary_purpose": "brief description of what the page does",
  "components_needed": [
    {{"type": "TextBox|Button|Dropdown|etc", "purpose": "what it's for", "label": "suggested label"}}
  ],
  "events_needed": [
    {{"name": "suggestedEventName", "trigger": "onClick|onSubmit|onLoad", "action": "what it should do"}}
  ],
  "data_bindings": [
    {{"path": "Page.fieldName", "purpose": "what data it holds"}}
  ],
  "layout_hints": {{
    "sections": ["header", "form", "footer"],
    "style": "centered|sidebar|fullwidth"
  }},
  "search_tags": ["tag1", "tag2", "tag3"]  // Tags to search pattern database
}}

Be specific about components and their purposes. Output valid JSON only."""

    def __init__(self, llm_client):
        self.llm = llm_client

    async def analyze(self, user_request: str) -> Dict[str, Any]:
        """Analyze user request and extract requirements"""
        prompt = self.ANALYSIS_PROMPT.format(request=user_request)

        # Call LLM (implementation depends on your client)
        response = await self.llm.generate(prompt)

        # Parse JSON response
        try:
            # Extract JSON from response
            json_str = response
            if "```json" in response:
                json_str = response.split("```json")[1].split("```")[0]
            elif "```" in response:
                json_str = response.split("```")[1].split("```")[0]

            return json.loads(json_str)
        except Exception as e:
            logger.error(f"Failed to parse analysis: {e}")
            return {
                "page_type": "other",
                "search_tags": user_request.lower().split()[:5],
                "components_needed": [],
                "events_needed": []
            }


class PatternAdapter:
    """
    Adapts retrieved patterns to match specific requirements.

    This is where the LLM modifies real patterns rather than generating from scratch.
    """

    ADAPT_PROMPT = """You are adapting a real, working page pattern to match new requirements.

IMPORTANT RULES:
1. Keep the EXACT structure of the pattern
2. Only change: labels, text content, binding paths, colors
3. DO NOT change: component types, children structure, event function structure
4. DO NOT add new structural elements
5. Output the COMPLETE adapted pattern as valid JSON

Original Pattern:
```json
{original_pattern}
```

Requirements to adapt to:
- Purpose: {purpose}
- Changes needed: {changes}

Output the adapted pattern as valid JSON. Keep all structural elements identical."""

    def __init__(self, llm_client):
        self.llm = llm_client

    async def adapt_pattern(
        self,
        pattern: Dict[str, Any],
        requirements: Dict[str, Any],
        changes: List[str]
    ) -> Dict[str, Any]:
        """Adapt a pattern to match requirements"""

        prompt = self.ADAPT_PROMPT.format(
            original_pattern=json.dumps(pattern, indent=2),
            purpose=requirements.get("primary_purpose", ""),
            changes="\n".join(f"- {c}" for c in changes)
        )

        response = await self.llm.generate(prompt)

        try:
            # Extract JSON
            json_str = response
            if "```json" in response:
                json_str = response.split("```json")[1].split("```")[0]
            elif "```" in response:
                json_str = response.split("```")[1].split("```")[0]

            adapted = json.loads(json_str)

            # Validate structure wasn't broken
            if self._validate_structure(pattern, adapted):
                return adapted
            else:
                logger.warning("Adaptation broke structure, returning original")
                return pattern

        except Exception as e:
            logger.error(f"Failed to adapt pattern: {e}")
            return pattern

    def _validate_structure(self, original: Dict, adapted: Dict) -> bool:
        """Validate that adaptation didn't break structure"""
        # Check component types are preserved
        orig_types = set()
        adapt_types = set()

        def collect_types(d, type_set):
            if isinstance(d, dict):
                if "type" in d:
                    type_set.add(d["type"])
                for v in d.values():
                    collect_types(v, type_set)
            elif isinstance(d, list):
                for item in d:
                    collect_types(item, type_set)

        collect_types(original, orig_types)
        collect_types(adapted, adapt_types)

        # All original types should be present
        return orig_types.issubset(adapt_types)


class PatternComposer:
    """
    Main orchestrator that composes patterns into complete pages.
    """

    def __init__(
        self,
        pattern_db: PatternDatabase,
        analyzer: SemanticAnalyzer,
        adapter: PatternAdapter
    ):
        self.db = pattern_db
        self.analyzer = analyzer
        self.adapter = adapter

    async def compose_page(
        self,
        user_request: str,
        existing_page: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Compose a page from patterns based on user request.

        Returns a complete, valid page definition.
        """

        # Step 1: Analyze the request
        logger.info("Analyzing user request...")
        requirements = await self.analyzer.analyze(user_request)
        logger.info(f"Requirements: {requirements}")

        # Step 2: Search for matching patterns
        logger.info("Searching for patterns...")
        search_tags = requirements.get("search_tags", [])

        # Find layout pattern
        layout_patterns = self.db.search(
            " ".join(search_tags),
            pattern_type="layout_structure",
            max_results=3
        )

        # Find form patterns if needed
        form_patterns = []
        if requirements.get("page_type") == "form" or any(
            c.get("type") in ["TextBox", "Dropdown", "Checkbox"]
            for c in requirements.get("components_needed", [])
        ):
            form_patterns = self.db.search(
                "form input submit",
                pattern_type="form_pattern",
                max_results=5
            )

        # Find event patterns
        event_patterns = []
        for event in requirements.get("events_needed", []):
            action = event.get("action", "")
            # Search for similar events
            matches = self.db.search(
                action,
                pattern_type="event_function",
                max_results=3
            )
            event_patterns.extend(matches)

        # Find component patterns
        component_patterns = self.db.search(
            " ".join(search_tags),
            pattern_type="component_tree",
            max_results=5
        )

        logger.info(f"Found: {len(layout_patterns)} layouts, {len(form_patterns)} forms, "
                   f"{len(event_patterns)} events, {len(component_patterns)} components")

        # Step 3: Select best patterns
        selected_layout = layout_patterns[0] if layout_patterns else None
        selected_form = self._select_best_form(form_patterns, requirements)
        selected_events = self._select_events(event_patterns, requirements)

        # Step 4: Compose the page
        page_def = await self._compose(
            requirements,
            selected_layout,
            selected_form,
            selected_events,
            component_patterns
        )

        # Step 5: Validate
        page_def = self._validate_and_fix(page_def)

        return page_def

    def _select_best_form(
        self,
        form_patterns: List[PatternMatch],
        requirements: Dict
    ) -> Optional[PatternMatch]:
        """Select the best form pattern based on field count"""
        if not form_patterns:
            return None

        needed_fields = len(requirements.get("components_needed", []))

        # Find form closest to needed field count
        best = None
        best_diff = float('inf')

        for fp in form_patterns:
            # Extract field count from tags like "4-fields"
            for tag in fp.tags:
                if tag.endswith("-fields"):
                    try:
                        count = int(tag.split("-")[0])
                        diff = abs(count - needed_fields)
                        if diff < best_diff:
                            best_diff = diff
                            best = fp
                    except:
                        pass

        return best or form_patterns[0]

    def _select_events(
        self,
        event_patterns: List[PatternMatch],
        requirements: Dict
    ) -> List[PatternMatch]:
        """Select relevant event patterns"""
        selected = []
        seen_purposes = set()

        for ep in event_patterns:
            # Deduplicate by semantic purpose
            purpose_key = tuple(sorted(ep.tags))
            if purpose_key not in seen_purposes:
                selected.append(ep)
                seen_purposes.add(purpose_key)

        return selected[:5]  # Limit to avoid complexity

    async def _compose(
        self,
        requirements: Dict,
        layout: Optional[PatternMatch],
        form: Optional[PatternMatch],
        events: List[PatternMatch],
        components: List[PatternMatch]
    ) -> Dict[str, Any]:
        """Compose patterns into a complete page"""

        page_def = {
            "name": requirements.get("primary_purpose", "generated_page")[:30].replace(" ", "_"),
            "rootComponent": "pageRoot",
            "componentDefinition": {},
            "eventFunctions": {},
            "properties": {}
        }

        # Start with layout skeleton
        if layout and layout.definition:
            skeleton = layout.definition.get("skeleton", {})
            for key, comp in skeleton.items():
                page_def["componentDefinition"][key] = comp.copy()

            root_key = layout.definition.get("rootComponent", "pageRoot")
            page_def["rootComponent"] = root_key
        else:
            # Create minimal structure
            page_def["componentDefinition"]["pageRoot"] = {
                "key": "pageRoot",
                "name": "Page Root",
                "type": "Grid",
                "children": {}
            }

        # Add form if we have one
        if form and form.definition:
            form_components = form.definition.get("components", {})

            # Adapt the form to requirements
            changes = [
                f"Change field labels to match: {[c.get('label') for c in requirements.get('components_needed', [])]}",
                f"Update binding paths to: {[b.get('path') for b in requirements.get('data_bindings', [])]}"
            ]

            adapted_form = await self.adapter.adapt_pattern(
                form_components,
                requirements,
                changes
            )

            # Merge into page
            page_def["componentDefinition"].update(adapted_form)

            # Add form root to page root children
            form_root = list(adapted_form.keys())[0] if adapted_form else None
            if form_root:
                root_key = page_def["rootComponent"]
                if root_key in page_def["componentDefinition"]:
                    if "children" not in page_def["componentDefinition"][root_key]:
                        page_def["componentDefinition"][root_key]["children"] = {}
                    page_def["componentDefinition"][root_key]["children"][form_root] = True

            # Add form's submit event
            submit_event = form.definition.get("submitEvent")
            if submit_event:
                event_key = f"onSubmit_{form_root}"
                page_def["eventFunctions"][event_key] = submit_event

        # Add other events
        for i, event in enumerate(events):
            if event.definition:
                event_key = event.definition.get("name", f"event_{i}")
                page_def["eventFunctions"][event_key] = event.definition

        return page_def

    def _validate_and_fix(self, page_def: Dict) -> Dict:
        """Validate and fix common issues"""

        # Fix 1: Ensure rootComponent is a string
        if isinstance(page_def.get("rootComponent"), dict):
            page_def["rootComponent"] = page_def["rootComponent"].get("key", "pageRoot")

        # Fix 2: Ensure all children references exist
        comp_def = page_def.get("componentDefinition", {})
        for comp_key, comp in list(comp_def.items()):
            if "children" in comp:
                valid_children = {}
                for child_key in comp["children"]:
                    if child_key in comp_def:
                        valid_children[child_key] = True
                    else:
                        logger.warning(f"Removing invalid child reference: {child_key}")
                comp["children"] = valid_children

        # Fix 3: Ensure component types are valid
        valid_types = {
            "Grid", "Text", "Button", "TextBox", "Checkbox",
            "RadioButton", "Dropdown", "Image", "Icon", "Link",
            "ArrayRepeater", "Form", "Popup", "Tabs", "Menu",
            "Table", "Video", "Audio", "Calendar", "Chart"
        }

        for comp_key, comp in comp_def.items():
            if comp.get("type") not in valid_types:
                logger.warning(f"Invalid component type '{comp.get('type')}' in {comp_key}, defaulting to Grid")
                comp["type"] = "Grid"

        # Fix 4: Ensure onClick is properly formatted
        for comp in comp_def.values():
            props = comp.get("properties", {})
            if "onClick" in props:
                onclick = props["onClick"]
                if isinstance(onclick, str):
                    props["onClick"] = {"value": onclick}

        return page_def


# Factory function to create the composer with your LLM client
def create_pattern_composer(
    patterns_dir: str,
    llm_client
) -> PatternComposer:
    """Create a PatternComposer with all dependencies"""

    db = PatternDatabase(patterns_dir)
    analyzer = SemanticAnalyzer(llm_client)
    adapter = PatternAdapter(llm_client)

    return PatternComposer(db, analyzer, adapter)


# Example integration with existing agent system
class PatternBasedPageAgent:
    """
    Replacement for the multi-agent page generation system.

    Uses pattern composition instead of from-scratch generation.
    """

    def __init__(self, patterns_dir: str, llm_client):
        self.composer = create_pattern_composer(patterns_dir, llm_client)

    async def generate(
        self,
        instruction: str,
        existing_page: Optional[Dict] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """Generate a page from instruction"""

        try:
            page_def = await self.composer.compose_page(
                instruction,
                existing_page
            )

            return {
                "success": True,
                "page": page_def,
                "method": "pattern_composition"
            }

        except Exception as e:
            logger.error(f"Pattern composition failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }
