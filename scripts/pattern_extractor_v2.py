"""
Pattern Extractor V2 - Enhanced with better semantic detection

Improvements over V1:
1. Better semantic detection (login, calculator, dashboard, etc.)
2. More pattern types (calculator, navigation, modal, carousel, etc.)
3. Pattern quality scoring
4. Deduplication by semantic similarity
5. Style pattern extraction (color schemes, spacing)
6. Animation pattern extraction
7. Data binding pattern extraction
"""

import json
import os
import hashlib
import re
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple, Set
from dataclasses import dataclass, field, asdict
from enum import Enum
from collections import defaultdict
import math


class PatternType(Enum):
    # Core patterns
    EVENT_FUNCTION = "event_function"
    COMPONENT_TREE = "component_tree"
    LAYOUT_STRUCTURE = "layout_structure"
    FORM_PATTERN = "form_pattern"

    # New pattern types
    CALCULATOR_PATTERN = "calculator_pattern"
    NAVIGATION_PATTERN = "navigation_pattern"
    MODAL_PATTERN = "modal_pattern"
    LIST_REPEATER_PATTERN = "list_repeater_pattern"
    CAROUSEL_PATTERN = "carousel_pattern"
    TABS_PATTERN = "tabs_pattern"
    DATA_FETCH_PATTERN = "data_fetch_pattern"
    AUTH_PATTERN = "auth_pattern"
    STYLE_THEME = "style_theme"
    ANIMATION_PATTERN = "animation_pattern"
    VALIDATION_PATTERN = "validation_pattern"


# Semantic detection rules
SEMANTIC_RULES = {
    # Page type detection
    "login": {
        "keywords": ["login", "signin", "sign in", "password", "username", "email", "authenticate"],
        "components": ["TextBox", "Button"],
        "events": ["Login", "SendData"],
        "bindings": ["password", "email", "username", "user"],
    },
    "registration": {
        "keywords": ["register", "signup", "sign up", "create account", "new user"],
        "components": ["TextBox", "Button", "Checkbox"],
        "events": ["register", "SignUp", "SendData"],
        "bindings": ["email", "password", "confirm", "name", "phone"],
    },
    "calculator": {
        "keywords": ["calculator", "calc", "emi", "loan", "interest", "compute", "calculate"],
        "components": ["TextBox", "Button", "Text"],
        "events": ["SetStore"],
        "bindings": ["display", "result", "total", "amount", "rate", "tenure"],
        "patterns": ["multiple similar buttons", "display area"],
    },
    "dashboard": {
        "keywords": ["dashboard", "overview", "summary", "stats", "analytics", "metrics"],
        "components": ["Grid", "Text", "Chart", "Table"],
        "events": ["FetchData"],
        "bindings": ["stats", "data", "metrics", "count"],
    },
    "listing": {
        "keywords": ["list", "table", "grid", "items", "products", "users", "records"],
        "components": ["ArrayRepeater", "Table", "Grid"],
        "events": ["FetchData", "DeleteData"],
        "bindings": ["items", "list", "data", "records"],
    },
    "detail": {
        "keywords": ["detail", "view", "profile", "info", "single", "item"],
        "components": ["Text", "Image", "Grid"],
        "events": ["FetchData"],
        "bindings": ["item", "detail", "data"],
    },
    "form": {
        "keywords": ["form", "input", "submit", "contact", "enquiry", "apply"],
        "components": ["TextBox", "Dropdown", "Checkbox", "Button"],
        "events": ["SendData", "SetStore"],
        "bindings": ["form", "data", "input"],
    },
    "navigation": {
        "keywords": ["nav", "menu", "header", "sidebar", "footer", "links"],
        "components": ["Grid", "Link", "Button", "Icon"],
        "events": ["Navigate"],
        "bindings": [],
    },
    "modal": {
        "keywords": ["modal", "popup", "dialog", "overlay", "confirm"],
        "components": ["Popup", "Grid", "Button"],
        "events": ["SetStore"],
        "bindings": ["show", "visible", "open", "modal"],
    },
    "carousel": {
        "keywords": ["carousel", "slider", "slideshow", "gallery", "swipe"],
        "components": ["Carousel", "Image", "Grid"],
        "events": ["SetStore"],
        "bindings": ["current", "index", "slide"],
    },
    "tabs": {
        "keywords": ["tabs", "tab", "switch", "toggle", "sections"],
        "components": ["Tabs", "Grid", "Button"],
        "events": ["SetStore"],
        "bindings": ["active", "selected", "tab"],
    },
    "search": {
        "keywords": ["search", "filter", "find", "query", "lookup"],
        "components": ["TextBox", "Button", "Grid"],
        "events": ["FetchData", "SetStore"],
        "bindings": ["search", "query", "filter", "results"],
    },
    "faq": {
        "keywords": ["faq", "accordion", "expand", "collapse", "question", "answer"],
        "components": ["Grid", "Text", "Button", "Icon"],
        "events": ["SetStore"],
        "bindings": ["expanded", "open", "faq"],
    },
}

# Event action detection
EVENT_ACTION_PATTERNS = {
    "toggle": {
        "pattern": r"(true|false)\s*\?\s*(false|true)\s*:\s*(true|false)",
        "description": "Toggle boolean value",
    },
    "increment": {
        "pattern": r"\+\s*1(?!\d)",
        "description": "Increment counter",
    },
    "decrement": {
        "pattern": r"-\s*1(?!\d)",
        "description": "Decrement counter",
    },
    "append": {
        "pattern": r"\+\s*['\"]",
        "description": "Append to string",
    },
    "concat": {
        "pattern": r"\+\s*['\"]\s*['\"]\s*\+",
        "description": "String concatenation",
    },
    "reset": {
        "pattern": r"['\"][\s]*['\"]|null|undefined|\[\]|\{\}|0(?!\d)",
        "description": "Reset to empty/default value",
    },
    "api_get": {
        "functions": ["FetchData"],
        "methods": ["GET"],
        "description": "Fetch data from API",
    },
    "api_post": {
        "functions": ["SendData"],
        "methods": ["POST"],
        "description": "Send data to API",
    },
    "api_delete": {
        "functions": ["DeleteData", "Delete"],
        "description": "Delete data via API",
    },
    "navigate": {
        "functions": ["Navigate"],
        "description": "Navigate to page",
    },
    "show_message": {
        "functions": ["Message"],
        "description": "Show user message",
    },
    "login": {
        "functions": ["Login"],
        "description": "User authentication",
    },
    "logout": {
        "functions": ["Logout"],
        "description": "User logout",
    },
    "conditional": {
        "functions": ["If"],
        "description": "Conditional branching",
    },
    "loop": {
        "functions": ["ForEachLoop", "CountLoop", "RangeLoop"],
        "description": "Iteration/looping",
    },
    "scroll": {
        "functions": ["ScrollTo"],
        "description": "Scroll to element",
    },
    "copy": {
        "functions": ["CopyTextToClipboard"],
        "description": "Copy to clipboard",
    },
}


@dataclass
class ExtractedPattern:
    """A single extracted pattern with enhanced metadata"""
    id: str
    type: PatternType
    name: str
    description: str
    semantic_tags: List[str]
    semantic_category: str  # login, calculator, form, etc.

    # The actual pattern data
    definition: Dict[str, Any]

    # Context about where it came from
    source_page: str
    source_app: str
    source_component_key: Optional[str] = None

    # Dependencies and relationships
    required_store_paths: List[str] = field(default_factory=list)
    produced_store_paths: List[str] = field(default_factory=list)
    referenced_events: List[str] = field(default_factory=list)
    referenced_components: List[str] = field(default_factory=list)

    # Complexity and quality metrics
    component_count: int = 0
    event_step_count: int = 0
    style_property_count: int = 0
    quality_score: float = 0.0  # 0-1 score

    # For deduplication
    semantic_hash: str = ""

    def to_dict(self) -> Dict:
        d = asdict(self)
        d['type'] = self.type.value
        return d


class SemanticDetector:
    """Detects semantic meaning from page content"""

    def __init__(self):
        self.rules = SEMANTIC_RULES

    def detect_page_type(self, page_def: Dict) -> Tuple[str, float]:
        """Detect the type of page and confidence score"""
        scores = defaultdict(float)

        page_name = page_def.get("name", "").lower()
        components = page_def.get("componentDefinition", {})
        events = page_def.get("eventFunctions", {})

        # Collect all text content
        all_text = self._collect_text(page_def).lower()

        # Collect component types
        comp_types = [c.get("type", "") for c in components.values()]

        # Collect event functions used
        event_funcs = []
        for evt in events.values():
            for step in evt.get("steps", {}).values():
                event_funcs.append(step.get("name", ""))

        # Collect binding paths
        bindings = []
        for comp in components.values():
            bp = comp.get("bindingPath", {})
            if isinstance(bp, dict) and bp.get("value"):
                bindings.append(bp["value"].lower())

        # Score each category
        for category, rules in self.rules.items():
            score = 0.0

            # Keyword matches in page name (high weight)
            for kw in rules["keywords"]:
                if kw in page_name:
                    score += 3.0

            # Keyword matches in content
            for kw in rules["keywords"]:
                if kw in all_text:
                    score += 1.0

            # Component type matches
            for comp_type in rules["components"]:
                count = comp_types.count(comp_type)
                score += min(count * 0.5, 2.0)

            # Event function matches
            for evt_func in rules.get("events", []):
                if evt_func in event_funcs:
                    score += 1.5

            # Binding path matches
            for binding_kw in rules.get("bindings", []):
                for bp in bindings:
                    if binding_kw in bp:
                        score += 1.0

            scores[category] = score

        # Get best match
        if scores:
            best_category = max(scores, key=scores.get)
            max_score = scores[best_category]
            # Normalize confidence
            confidence = min(max_score / 10.0, 1.0)
            return best_category, confidence

        return "other", 0.0

    def _collect_text(self, obj: Any, depth: int = 0) -> str:
        """Recursively collect all text content"""
        if depth > 10:
            return ""

        texts = []

        if isinstance(obj, dict):
            # Collect specific text fields
            for key in ["text", "label", "placeholder", "name", "title"]:
                if key in obj:
                    val = obj[key]
                    if isinstance(val, str):
                        texts.append(val)
                    elif isinstance(val, dict) and "value" in val:
                        texts.append(str(val["value"]))

            for v in obj.values():
                texts.append(self._collect_text(v, depth + 1))

        elif isinstance(obj, list):
            for item in obj:
                texts.append(self._collect_text(item, depth + 1))

        return " ".join(texts)

    def detect_event_actions(self, event_def: Dict) -> List[str]:
        """Detect what actions an event performs"""
        actions = []

        steps = event_def.get("steps", {})

        # Collect all expressions
        all_expressions = []
        all_functions = []

        for step in steps.values():
            func_name = step.get("name", "")
            all_functions.append(func_name)

            # Collect expressions from parameterMap
            param_map = step.get("parameterMap", {})
            self._collect_expressions(param_map, all_expressions)

        # Check pattern-based actions
        for action, rules in EVENT_ACTION_PATTERNS.items():
            if "pattern" in rules:
                for expr in all_expressions:
                    if re.search(rules["pattern"], expr):
                        actions.append(action)
                        break

            if "functions" in rules:
                for func in rules["functions"]:
                    if func in all_functions:
                        actions.append(action)
                        break

        return list(set(actions))

    def _collect_expressions(self, obj: Any, expressions: List[str]):
        """Collect all expressions from an object"""
        if isinstance(obj, dict):
            if "expression" in obj:
                expressions.append(str(obj["expression"]))
            for v in obj.values():
                self._collect_expressions(v, expressions)
        elif isinstance(obj, list):
            for item in obj:
                self._collect_expressions(item, expressions)


class PatternQualityScorer:
    """Scores patterns for quality and usefulness"""

    def score_event_pattern(self, pattern: Dict, analysis: Dict) -> float:
        """Score an event pattern for quality"""
        score = 0.5  # Base score

        steps = pattern.get("steps", {})
        step_count = len(steps)

        # Prefer patterns with 1-5 steps (not too simple, not too complex)
        if 1 <= step_count <= 3:
            score += 0.2
        elif 3 < step_count <= 5:
            score += 0.1
        elif step_count > 10:
            score -= 0.2

        # Bonus for having clear dependencies
        has_deps = any(s.get("dependentStatements") for s in steps.values())
        if has_deps and step_count > 1:
            score += 0.1

        # Bonus for common useful patterns
        actions = analysis.get("actions", [])
        useful_actions = {"api_get", "api_post", "navigate", "toggle", "login"}
        if any(a in useful_actions for a in actions):
            score += 0.15

        # Penalty for conversion errors (from JS2KIRun)
        if any("_conversion_error" in str(s) for s in steps.values()):
            score -= 0.3

        return max(0.0, min(1.0, score))

    def score_component_pattern(self, pattern: Dict, analysis: Dict) -> float:
        """Score a component pattern for quality"""
        score = 0.5

        comp_count = analysis.get("component_count", 0)

        # Prefer medium-sized patterns (3-15 components)
        if 3 <= comp_count <= 8:
            score += 0.2
        elif 8 < comp_count <= 15:
            score += 0.1
        elif comp_count > 30:
            score -= 0.2

        # Bonus for having styles
        if analysis.get("style_count", 0) > 0:
            score += 0.1

        # Bonus for responsive styles
        if analysis.get("has_responsive", False):
            score += 0.1

        # Bonus for semantic naming
        if analysis.get("has_semantic_names", False):
            score += 0.1

        return max(0.0, min(1.0, score))


class EnhancedPatternExtractor:
    """Enhanced pattern extractor with semantic detection"""

    def __init__(self):
        self.patterns: List[ExtractedPattern] = []
        self.semantic_detector = SemanticDetector()
        self.quality_scorer = PatternQualityScorer()
        self.seen_hashes: Set[str] = set()

        # Statistics
        self.stats = {
            "pages_processed": 0,
            "patterns_extracted": 0,
            "patterns_deduplicated": 0,
            "by_category": defaultdict(int),
            "by_type": defaultdict(int),
            "event_functions_used": defaultdict(int),
            "component_types_used": defaultdict(int),
        }

    def extract_from_page(self, page_def: Dict, source_info: Dict) -> List[ExtractedPattern]:
        """Extract all patterns from a page definition"""
        patterns = []

        self.stats["pages_processed"] += 1

        # Detect page type
        page_type, confidence = self.semantic_detector.detect_page_type(page_def)
        source_info["page_type"] = page_type
        source_info["page_type_confidence"] = confidence

        # 1. Extract event function patterns
        event_patterns = self._extract_event_patterns(
            page_def.get("eventFunctions", {}),
            page_def,
            source_info
        )
        patterns.extend(event_patterns)

        # 2. Extract component tree patterns
        component_patterns = self._extract_component_patterns(
            page_def.get("componentDefinition", {}),
            page_def.get("rootComponent", ""),
            source_info
        )
        patterns.extend(component_patterns)

        # 3. Extract form patterns
        form_patterns = self._extract_form_patterns(
            page_def.get("componentDefinition", {}),
            page_def.get("eventFunctions", {}),
            source_info
        )
        patterns.extend(form_patterns)

        # 4. Extract calculator patterns
        calc_patterns = self._extract_calculator_patterns(
            page_def.get("componentDefinition", {}),
            page_def.get("eventFunctions", {}),
            source_info
        )
        patterns.extend(calc_patterns)

        # 5. Extract navigation patterns
        nav_patterns = self._extract_navigation_patterns(
            page_def.get("componentDefinition", {}),
            page_def.get("eventFunctions", {}),
            source_info
        )
        patterns.extend(nav_patterns)

        # 6. Extract modal/popup patterns
        modal_patterns = self._extract_modal_patterns(
            page_def.get("componentDefinition", {}),
            page_def.get("eventFunctions", {}),
            source_info
        )
        patterns.extend(modal_patterns)

        # 7. Extract list/repeater patterns
        list_patterns = self._extract_list_patterns(
            page_def.get("componentDefinition", {}),
            page_def.get("eventFunctions", {}),
            source_info
        )
        patterns.extend(list_patterns)

        # 8. Extract layout patterns
        layout_patterns = self._extract_layout_patterns(
            page_def.get("componentDefinition", {}),
            page_def.get("rootComponent", ""),
            source_info
        )
        patterns.extend(layout_patterns)

        # 9. Extract authentication patterns
        auth_patterns = self._extract_auth_patterns(
            page_def.get("eventFunctions", {}),
            page_def.get("componentDefinition", {}),
            source_info
        )
        patterns.extend(auth_patterns)

        # 10. Extract data fetch patterns
        fetch_patterns = self._extract_data_fetch_patterns(
            page_def.get("eventFunctions", {}),
            source_info
        )
        patterns.extend(fetch_patterns)

        # 11. Extract style/theme patterns
        style_patterns = self._extract_style_patterns(
            page_def.get("componentDefinition", {}),
            source_info
        )
        patterns.extend(style_patterns)

        # Deduplicate
        unique_patterns = self._deduplicate(patterns)

        self.stats["patterns_extracted"] += len(patterns)
        self.stats["patterns_deduplicated"] += len(patterns) - len(unique_patterns)

        self.patterns.extend(unique_patterns)
        return unique_patterns

    def _extract_event_patterns(
        self,
        event_functions: Dict[str, Any],
        page_def: Dict,
        source_info: Dict
    ) -> List[ExtractedPattern]:
        """Extract event function patterns with semantic analysis"""
        patterns = []

        for event_key, event_def in event_functions.items():
            if not isinstance(event_def, dict):
                continue

            steps = event_def.get("steps", {})
            if not steps:
                continue

            # Analyze the event
            analysis = self._analyze_event_function(event_def)
            actions = self.semantic_detector.detect_event_actions(event_def)
            analysis["actions"] = actions

            # Generate semantic tags
            tags = self._generate_event_tags(analysis, actions)

            # Determine semantic category
            category = self._categorize_event(analysis, actions, source_info)

            # Score quality
            quality = self.quality_scorer.score_event_pattern(event_def, analysis)

            # Clean the definition
            cleaned_def = self._clean_event_definition(event_def)

            # Generate semantic hash for deduplication
            semantic_hash = self._hash_event(cleaned_def, tags)

            pattern = ExtractedPattern(
                id=self._generate_id(f"event_{event_key}_{source_info['page']}"),
                type=PatternType.EVENT_FUNCTION,
                name=event_def.get("name", event_key),
                description=self._generate_event_description(analysis, actions),
                semantic_tags=tags,
                semantic_category=category,
                definition=cleaned_def,
                source_page=source_info["page"],
                source_app=source_info["app"],
                required_store_paths=analysis.get("reads_from", []),
                produced_store_paths=analysis.get("writes_to", []),
                event_step_count=len(steps),
                quality_score=quality,
                semantic_hash=semantic_hash
            )

            patterns.append(pattern)

            # Update stats
            for step in steps.values():
                func_name = step.get("name", "unknown")
                self.stats["event_functions_used"][func_name] += 1

        return patterns

    def _analyze_event_function(self, event_def: Dict) -> Dict:
        """Deeply analyze an event function"""
        analysis = {
            "functions_used": [],
            "has_api_call": False,
            "has_conditional": False,
            "has_navigation": False,
            "has_loop": False,
            "has_validation": False,
            "reads_from": [],
            "writes_to": [],
            "api_endpoints": [],
            "api_methods": [],
            "dependency_depth": 0,
            "complexity": "simple"
        }

        steps = event_def.get("steps", {})

        # Build dependency graph
        dep_graph = {}
        for step_key, step in steps.items():
            deps = step.get("dependentStatements", {})
            dep_graph[step_key] = list(deps.keys()) if deps else []

        # Calculate dependency depth
        analysis["dependency_depth"] = self._calculate_dep_depth(dep_graph)

        for step_key, step in steps.items():
            func_name = step.get("name", "")
            namespace = step.get("namespace", "")

            analysis["functions_used"].append(f"{namespace}.{func_name}")

            # Detect patterns
            if func_name in ["SendData", "FetchData"]:
                analysis["has_api_call"] = True
                param_map = step.get("parameterMap", {})

                # Extract endpoint
                url_param = param_map.get("url", {})
                for param in url_param.values():
                    if param.get("value"):
                        analysis["api_endpoints"].append(param["value"])
                    elif param.get("expression"):
                        analysis["api_endpoints"].append(param["expression"])

                # Extract method
                method_param = param_map.get("method", {})
                for param in method_param.values():
                    if param.get("value"):
                        analysis["api_methods"].append(param["value"])

            if func_name == "If":
                analysis["has_conditional"] = True

            if func_name == "Navigate":
                analysis["has_navigation"] = True

            if func_name in ["ForEachLoop", "CountLoop", "RangeLoop"]:
                analysis["has_loop"] = True

            if func_name in ["Login", "Logout"]:
                analysis["has_auth"] = True

            # Track store access
            param_map = step.get("parameterMap", {})

            if func_name == "SetStore":
                path_param = param_map.get("path", {})
                for p in path_param.values():
                    if p.get("value"):
                        analysis["writes_to"].append(p["value"])

            self._extract_store_reads(param_map, analysis["reads_from"])

        # Determine complexity
        step_count = len(steps)
        if step_count > 7 or analysis["has_loop"]:
            analysis["complexity"] = "complex"
        elif step_count > 3 or analysis["has_conditional"] or analysis["has_api_call"]:
            analysis["complexity"] = "moderate"

        return analysis

    def _calculate_dep_depth(self, dep_graph: Dict) -> int:
        """Calculate the maximum dependency chain depth"""
        depths = {}

        def get_depth(node, visited=None):
            if visited is None:
                visited = set()

            if node in visited:
                return 0  # Cycle detected

            if node in depths:
                return depths[node]

            visited.add(node)
            max_dep_depth = 0

            for dep in dep_graph.get(node, []):
                # Extract step name from "Steps.stepName.output"
                parts = dep.split(".")
                if len(parts) >= 2:
                    step_name = parts[1]
                    dep_depth = get_depth(step_name, visited.copy())
                    max_dep_depth = max(max_dep_depth, dep_depth + 1)

            depths[node] = max_dep_depth
            return max_dep_depth

        max_depth = 0
        for node in dep_graph:
            max_depth = max(max_depth, get_depth(node))

        return max_depth

    def _extract_store_reads(self, param_map: Dict, reads_list: List[str]):
        """Extract all store paths that are read in expressions"""
        store_pattern = re.compile(
            r'(Page\.[a-zA-Z0-9_.]+|Store\.[a-zA-Z0-9_.]+|'
            r'LocalStore\.[a-zA-Z0-9_.]+|Steps\.[a-zA-Z0-9_.]+|Parent\.[a-zA-Z0-9_.]+)'
        )

        def extract_from_value(val):
            if isinstance(val, str):
                matches = store_pattern.findall(val)
                reads_list.extend(matches)
            elif isinstance(val, dict):
                for k, v in val.items():
                    if k == "expression" and isinstance(v, str):
                        matches = store_pattern.findall(v)
                        reads_list.extend(matches)
                    else:
                        extract_from_value(v)
            elif isinstance(val, list):
                for item in val:
                    extract_from_value(item)

        extract_from_value(param_map)

    def _generate_event_tags(self, analysis: Dict, actions: List[str]) -> List[str]:
        """Generate semantic tags for an event"""
        tags = list(actions)  # Start with detected actions

        if analysis["has_api_call"]:
            tags.append("api")
            tags.append("async")

            for endpoint in analysis["api_endpoints"]:
                endpoint_lower = endpoint.lower() if isinstance(endpoint, str) else ""
                if "login" in endpoint_lower:
                    tags.extend(["login", "authentication"])
                elif "user" in endpoint_lower:
                    tags.append("user-management")
                elif "register" in endpoint_lower:
                    tags.append("registration")
                elif any(x in endpoint_lower for x in ["save", "create", "update", "post"]):
                    tags.append("data-mutation")
                elif any(x in endpoint_lower for x in ["get", "fetch", "list", "find"]):
                    tags.append("data-fetch")

        if analysis["has_conditional"]:
            tags.append("conditional")
            tags.append("branching")

        if analysis["has_navigation"]:
            tags.append("navigation")
            tags.append("routing")

        if analysis["has_loop"]:
            tags.append("iteration")
            tags.append("loop")

        if analysis.get("has_auth"):
            tags.append("authentication")

        # Detect specific patterns from writes
        writes = [w.lower() for w in analysis.get("writes_to", [])]

        if any("display" in w for w in writes):
            tags.append("display-update")

        if any("form" in w for w in writes):
            tags.append("form")

        if any("error" in w for w in writes):
            tags.append("error-handling")

        if any("loading" in w or "isloading" in w for w in writes):
            tags.append("loading-state")

        if any("modal" in w or "popup" in w or "show" in w for w in writes):
            tags.append("modal-control")

        tags.append(analysis["complexity"])

        return list(set(tags))

    def _categorize_event(
        self,
        analysis: Dict,
        actions: List[str],
        source_info: Dict
    ) -> str:
        """Determine the semantic category of an event"""

        # Check for specific patterns
        if "login" in actions or analysis.get("has_auth"):
            return "authentication"

        if "api_post" in actions:
            return "data-mutation"

        if "api_get" in actions:
            return "data-fetch"

        if "navigate" in actions:
            return "navigation"

        if "toggle" in actions:
            return "ui-toggle"

        if "increment" in actions or "decrement" in actions:
            return "counter"

        if "append" in actions:
            return "calculator"

        # Fall back to page type
        return source_info.get("page_type", "other")

    def _generate_event_description(self, analysis: Dict, actions: List[str]) -> str:
        """Generate a human-readable description"""
        parts = []

        # Describe main action
        if "api_post" in actions:
            endpoints = analysis.get("api_endpoints", [])
            if endpoints:
                parts.append(f"Posts to API: {endpoints[0][:50]}")
            else:
                parts.append("Posts data to API")
        elif "api_get" in actions:
            parts.append("Fetches data from API")
        elif "login" in actions:
            parts.append("Handles user login")
        elif "navigate" in actions:
            parts.append("Navigates to another page")
        elif "toggle" in actions:
            parts.append("Toggles boolean state")
        elif "increment" in actions:
            parts.append("Increments counter")
        elif "append" in actions:
            parts.append("Appends to value")

        # Describe conditional
        if analysis["has_conditional"]:
            parts.append("with conditional logic")

        # Describe what it updates
        writes = analysis.get("writes_to", [])[:3]
        if writes:
            parts.append(f"Updates: {', '.join(writes)}")

        return ". ".join(parts) if parts else "Performs store update"

    def _clean_event_definition(self, event_def: Dict) -> Dict:
        """Clean an event definition for storage"""
        cleaned = {
            "name": event_def.get("name"),
            "namespace": event_def.get("namespace", ""),
            "steps": {}
        }

        if event_def.get("validationCheck"):
            cleaned["validationCheck"] = "__COMPONENT_KEY__"

        for step_key, step in event_def.get("steps", {}).items():
            cleaned_step = {
                "statementName": step.get("statementName"),
                "name": step.get("name"),
                "namespace": step.get("namespace"),
            }

            if "parameterMap" in step:
                cleaned_step["parameterMap"] = self._simplify_param_map(
                    step["parameterMap"]
                )

            if "dependentStatements" in step:
                cleaned_step["dependentStatements"] = step["dependentStatements"]

            cleaned["steps"][step_key] = cleaned_step

        return cleaned

    def _simplify_param_map(self, param_map: Dict) -> Dict:
        """Simplify parameter map keys"""
        simplified = {}

        for param_name, param_values in param_map.items():
            simplified[param_name] = {}

            if isinstance(param_values, dict):
                for i, (key, val) in enumerate(param_values.items()):
                    new_key = f"p{i+1}"
                    if isinstance(val, dict):
                        simplified_val = {
                            "key": new_key,
                            "type": val.get("type"),
                            "order": val.get("order", i + 1)
                        }
                        if val.get("value") is not None:
                            simplified_val["value"] = val["value"]
                        if val.get("expression"):
                            simplified_val["expression"] = val["expression"]
                        simplified[param_name][new_key] = simplified_val
                    else:
                        simplified[param_name][new_key] = val

        return simplified

    def _hash_event(self, event_def: Dict, tags: List[str]) -> str:
        """Generate a semantic hash for deduplication"""
        # Hash based on structure, not specific values
        steps = event_def.get("steps", {})

        structure = []
        for step in steps.values():
            structure.append(f"{step.get('namespace')}.{step.get('name')}")

        structure.sort()
        structure_str = "|".join(structure) + "|" + ",".join(sorted(tags))

        return hashlib.md5(structure_str.encode()).hexdigest()[:8]

    def _extract_calculator_patterns(
        self,
        component_def: Dict,
        event_functions: Dict,
        source_info: Dict
    ) -> List[ExtractedPattern]:
        """Extract calculator-specific patterns"""
        patterns = []

        # Look for calculator indicators
        calc_events = []
        calc_bindings = []

        for event_key, event_def in event_functions.items():
            event_name = event_def.get("name", event_key).lower()

            # Look for digit/operator patterns
            if any(x in event_name for x in ["digit", "number", "operator", "calculate", "clear", "equals"]):
                calc_events.append((event_key, event_def))
                continue

            # Look for patterns like "append" + number
            if re.search(r'(append|add|press)\d', event_name):
                calc_events.append((event_key, event_def))

        # Look for display bindings
        for comp in component_def.values():
            bp = comp.get("bindingPath", {})
            if isinstance(bp, dict):
                path = bp.get("value", "").lower()
                if any(x in path for x in ["display", "result", "total", "value"]):
                    calc_bindings.append(bp.get("value"))

        # If we have calculator-like events, extract the pattern
        if len(calc_events) >= 3:  # At least 3 digit/operator buttons
            # Group similar events
            digit_events = {}
            operator_events = {}
            control_events = {}

            for event_key, event_def in calc_events:
                name = event_def.get("name", "").lower()

                if re.search(r'\d', name):
                    digit_events[event_key] = event_def
                elif any(x in name for x in ["add", "subtract", "multiply", "divide", "plus", "minus"]):
                    operator_events[event_key] = event_def
                else:
                    control_events[event_key] = event_def

            # Extract digit pattern (just one example)
            if digit_events:
                example_event = list(digit_events.values())[0]

                pattern = ExtractedPattern(
                    id=self._generate_id(f"calc_digit_{source_info['page']}"),
                    type=PatternType.CALCULATOR_PATTERN,
                    name="Calculator Digit Pattern",
                    description=f"Calculator with {len(digit_events)} digit buttons",
                    semantic_tags=["calculator", "digit", "append", "display-update"],
                    semantic_category="calculator",
                    definition={
                        "exampleEvent": self._clean_event_definition(example_event),
                        "digitCount": len(digit_events),
                        "bindings": calc_bindings,
                        "patternDescription": "Appends digit to display value"
                    },
                    source_page=source_info["page"],
                    source_app=source_info["app"],
                    quality_score=0.8
                )
                patterns.append(pattern)

            # Extract operator pattern
            if operator_events:
                example_event = list(operator_events.values())[0]

                pattern = ExtractedPattern(
                    id=self._generate_id(f"calc_operator_{source_info['page']}"),
                    type=PatternType.CALCULATOR_PATTERN,
                    name="Calculator Operator Pattern",
                    description=f"Calculator with {len(operator_events)} operator buttons",
                    semantic_tags=["calculator", "operator", "math"],
                    semantic_category="calculator",
                    definition={
                        "exampleEvent": self._clean_event_definition(example_event),
                        "operatorCount": len(operator_events),
                        "bindings": calc_bindings
                    },
                    source_page=source_info["page"],
                    source_app=source_info["app"],
                    quality_score=0.8
                )
                patterns.append(pattern)

        return patterns

    def _extract_navigation_patterns(
        self,
        component_def: Dict,
        event_functions: Dict,
        source_info: Dict
    ) -> List[ExtractedPattern]:
        """Extract navigation patterns (menus, navbars, etc.)"""
        patterns = []

        # Find navigation containers
        for comp_key, comp in component_def.items():
            comp_name = comp.get("name", "").lower()
            comp_type = comp.get("type", "")

            # Look for nav indicators
            is_nav = any(x in comp_name for x in ["nav", "menu", "header", "sidebar", "footer"])

            if not is_nav:
                continue

            children = comp.get("children", {})
            if len(children) < 2:
                continue

            # Check if children are links or buttons
            link_count = 0
            button_count = 0

            for child_key in children:
                child = component_def.get(child_key, {})
                child_type = child.get("type", "")

                if child_type == "Link":
                    link_count += 1
                elif child_type == "Button":
                    props = child.get("properties", {})
                    if props.get("linkPath") or props.get("onClick"):
                        button_count += 1

            if link_count + button_count >= 2:
                # Extract the nav pattern
                nav_components = self._extract_subtree(comp_key, component_def)

                pattern = ExtractedPattern(
                    id=self._generate_id(f"nav_{comp_key}_{source_info['page']}"),
                    type=PatternType.NAVIGATION_PATTERN,
                    name=f"Navigation: {comp.get('name', comp_key)}",
                    description=f"Navigation with {link_count} links, {button_count} buttons",
                    semantic_tags=["navigation", "menu", "links", comp_name.split()[0] if comp_name else "nav"],
                    semantic_category="navigation",
                    definition={
                        "rootKey": comp_key,
                        "components": nav_components,
                        "linkCount": link_count,
                        "buttonCount": button_count
                    },
                    source_page=source_info["page"],
                    source_app=source_info["app"],
                    source_component_key=comp_key,
                    component_count=len(nav_components),
                    quality_score=0.7
                )
                patterns.append(pattern)

        return patterns

    def _extract_modal_patterns(
        self,
        component_def: Dict,
        event_functions: Dict,
        source_info: Dict
    ) -> List[ExtractedPattern]:
        """Extract modal/popup patterns"""
        patterns = []

        for comp_key, comp in component_def.items():
            comp_type = comp.get("type", "")

            if comp_type != "Popup":
                continue

            # Extract the popup and its children
            popup_components = self._extract_subtree(comp_key, component_def)

            # Find binding for show/hide
            binding = comp.get("bindingPath", {})
            binding_path = binding.get("value", "") if isinstance(binding, dict) else ""

            # Find related events (show/hide popup)
            related_events = {}
            for event_key, event_def in event_functions.items():
                writes = []
                for step in event_def.get("steps", {}).values():
                    param_map = step.get("parameterMap", {})
                    path_param = param_map.get("path", {})
                    for p in path_param.values():
                        if p.get("value"):
                            writes.append(p["value"])

                if binding_path and binding_path in writes:
                    related_events[event_key] = self._clean_event_definition(event_def)

            pattern = ExtractedPattern(
                id=self._generate_id(f"modal_{comp_key}_{source_info['page']}"),
                type=PatternType.MODAL_PATTERN,
                name=f"Modal: {comp.get('name', comp_key)}",
                description=f"Modal popup with {len(popup_components) - 1} child components",
                semantic_tags=["modal", "popup", "dialog", "overlay"],
                semantic_category="modal",
                definition={
                    "rootKey": comp_key,
                    "components": popup_components,
                    "bindingPath": binding_path,
                    "showHideEvents": related_events
                },
                source_page=source_info["page"],
                source_app=source_info["app"],
                source_component_key=comp_key,
                component_count=len(popup_components),
                quality_score=0.75
            )
            patterns.append(pattern)

        return patterns

    def _extract_list_patterns(
        self,
        component_def: Dict,
        event_functions: Dict,
        source_info: Dict
    ) -> List[ExtractedPattern]:
        """Extract list/repeater patterns"""
        patterns = []

        for comp_key, comp in component_def.items():
            comp_type = comp.get("type", "")

            if comp_type != "ArrayRepeater":
                continue

            # Extract the repeater and its template
            repeater_components = self._extract_subtree(comp_key, component_def)

            # Get binding
            binding = comp.get("bindingPath", {})
            binding_path = binding.get("value", "") if isinstance(binding, dict) else ""

            # Find item template (first child)
            children = list(comp.get("children", {}).keys())
            template_key = children[0] if children else None

            pattern = ExtractedPattern(
                id=self._generate_id(f"list_{comp_key}_{source_info['page']}"),
                type=PatternType.LIST_REPEATER_PATTERN,
                name=f"List: {comp.get('name', comp_key)}",
                description=f"List repeater bound to {binding_path}",
                semantic_tags=["list", "repeater", "array", "iteration"],
                semantic_category="listing",
                definition={
                    "rootKey": comp_key,
                    "components": repeater_components,
                    "bindingPath": binding_path,
                    "templateKey": template_key
                },
                source_page=source_info["page"],
                source_app=source_info["app"],
                source_component_key=comp_key,
                component_count=len(repeater_components),
                quality_score=0.8
            )
            patterns.append(pattern)

        return patterns

    def _extract_auth_patterns(
        self,
        event_functions: Dict,
        component_def: Dict,
        source_info: Dict
    ) -> List[ExtractedPattern]:
        """Extract authentication patterns"""
        patterns = []

        for event_key, event_def in event_functions.items():
            steps = event_def.get("steps", {})

            # Look for Login function
            has_login = False
            has_validation = False

            for step in steps.values():
                func_name = step.get("name", "")
                if func_name == "Login":
                    has_login = True
                if func_name in ["If", "Message"]:
                    has_validation = True

            if has_login:
                # Find related form components
                validation_check = event_def.get("validationCheck")
                form_components = {}

                if validation_check and validation_check in component_def:
                    form_components = self._extract_subtree(validation_check, component_def)

                pattern = ExtractedPattern(
                    id=self._generate_id(f"auth_{event_key}_{source_info['page']}"),
                    type=PatternType.AUTH_PATTERN,
                    name=f"Authentication: {event_def.get('name', event_key)}",
                    description="Login authentication with validation" if has_validation else "Login authentication",
                    semantic_tags=["authentication", "login", "security"],
                    semantic_category="authentication",
                    definition={
                        "event": self._clean_event_definition(event_def),
                        "formComponents": form_components,
                        "hasValidation": has_validation
                    },
                    source_page=source_info["page"],
                    source_app=source_info["app"],
                    event_step_count=len(steps),
                    quality_score=0.9
                )
                patterns.append(pattern)

        return patterns

    def _extract_data_fetch_patterns(
        self,
        event_functions: Dict,
        source_info: Dict
    ) -> List[ExtractedPattern]:
        """Extract data fetching patterns"""
        patterns = []

        for event_key, event_def in event_functions.items():
            steps = event_def.get("steps", {})

            fetch_steps = []
            store_steps = []

            for step_key, step in steps.items():
                func_name = step.get("name", "")

                if func_name in ["FetchData", "SendData"]:
                    fetch_steps.append(step)
                elif func_name == "SetStore":
                    store_steps.append(step)

            # If we have fetch + store, it's a data fetch pattern
            if fetch_steps and store_steps:
                # Analyze the pattern
                has_error_handling = any(
                    s.get("name") == "If" or s.get("name") == "Message"
                    for s in steps.values()
                )

                has_loading_state = any(
                    "loading" in str(s.get("parameterMap", {})).lower()
                    for s in steps.values()
                )

                pattern = ExtractedPattern(
                    id=self._generate_id(f"fetch_{event_key}_{source_info['page']}"),
                    type=PatternType.DATA_FETCH_PATTERN,
                    name=f"Data Fetch: {event_def.get('name', event_key)}",
                    description=f"Fetches data and stores result" +
                               (" with error handling" if has_error_handling else "") +
                               (" with loading state" if has_loading_state else ""),
                    semantic_tags=["data-fetch", "api", "async"] +
                                 (["error-handling"] if has_error_handling else []) +
                                 (["loading-state"] if has_loading_state else []),
                    semantic_category="data-fetch",
                    definition=self._clean_event_definition(event_def),
                    source_page=source_info["page"],
                    source_app=source_info["app"],
                    event_step_count=len(steps),
                    quality_score=0.85 if has_error_handling else 0.7
                )
                patterns.append(pattern)

        return patterns

    def _extract_style_patterns(
        self,
        component_def: Dict,
        source_info: Dict
    ) -> List[ExtractedPattern]:
        """Extract style/theme patterns"""
        patterns = []

        # Collect unique style configurations
        color_schemes = defaultdict(list)

        for comp_key, comp in component_def.items():
            style_props = comp.get("styleProperties", {})

            for style_key, style in style_props.items():
                resolutions = style.get("resolutions", {})
                all_res = resolutions.get("ALL", {})

                # Extract colors
                colors = []
                for prop_name in ["backgroundColor", "color", "borderColor"]:
                    if prop_name in all_res:
                        color_val = all_res[prop_name].get("value")
                        if color_val and color_val.startswith("#"):
                            colors.append((prop_name, color_val))

                if colors:
                    color_key = tuple(sorted(colors))
                    color_schemes[color_key].append({
                        "component": comp_key,
                        "type": comp.get("type"),
                        "style": all_res
                    })

        # Create patterns for common color schemes
        for color_scheme, usages in color_schemes.items():
            if len(usages) >= 2:  # Used in at least 2 components
                colors_dict = dict(color_scheme)

                # Determine if dark or light theme
                bg_color = colors_dict.get("backgroundColor", "#ffffff")
                is_dark = self._is_dark_color(bg_color)

                pattern = ExtractedPattern(
                    id=self._generate_id(f"style_{hash(color_scheme)}_{source_info['page']}"),
                    type=PatternType.STYLE_THEME,
                    name=f"{'Dark' if is_dark else 'Light'} Theme Pattern",
                    description=f"Color scheme used in {len(usages)} components",
                    semantic_tags=["style", "theme", "dark" if is_dark else "light", "colors"],
                    semantic_category="style",
                    definition={
                        "colors": colors_dict,
                        "exampleUsages": usages[:3],
                        "usageCount": len(usages)
                    },
                    source_page=source_info["page"],
                    source_app=source_info["app"],
                    quality_score=min(0.5 + len(usages) * 0.1, 0.9)
                )
                patterns.append(pattern)

        return patterns

    def _is_dark_color(self, hex_color: str) -> bool:
        """Determine if a color is dark"""
        try:
            hex_color = hex_color.lstrip("#")
            if len(hex_color) == 3:
                hex_color = "".join(c * 2 for c in hex_color)

            r = int(hex_color[0:2], 16)
            g = int(hex_color[2:4], 16)
            b = int(hex_color[4:6], 16)

            # Calculate relative luminance
            luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255

            return luminance < 0.5
        except:
            return False

    def _extract_component_patterns(
        self,
        component_def: Dict,
        root_component: str,
        source_info: Dict
    ) -> List[ExtractedPattern]:
        """Extract component tree patterns"""
        patterns = []

        for comp_key, comp in component_def.items():
            children = comp.get("children", {})

            if not children or len(children) < 2:
                continue

            # Analyze the subtree
            subtree = self._extract_subtree(comp_key, component_def)
            analysis = self._analyze_component_subtree(subtree, component_def)

            if analysis["is_generic"]:
                continue

            tags = self._generate_component_tags(analysis, source_info)

            # Score quality
            quality = self.quality_scorer.score_component_pattern(subtree, analysis)

            # Generate semantic hash
            semantic_hash = self._hash_component_tree(subtree, tags)

            pattern = ExtractedPattern(
                id=self._generate_id(f"comp_{comp_key}_{source_info['page']}"),
                type=PatternType.COMPONENT_TREE,
                name=comp.get("name", comp_key),
                description=self._generate_component_description(analysis),
                semantic_tags=tags,
                semantic_category=source_info.get("page_type", "other"),
                definition={"rootKey": comp_key, "components": subtree},
                source_page=source_info["page"],
                source_app=source_info["app"],
                source_component_key=comp_key,
                required_store_paths=analysis.get("bindings", []),
                referenced_events=analysis.get("events", []),
                component_count=len(subtree),
                style_property_count=analysis.get("style_count", 0),
                quality_score=quality,
                semantic_hash=semantic_hash
            )

            patterns.append(pattern)

            # Update stats
            for c in subtree.values():
                self.stats["component_types_used"][c.get("type", "unknown")] += 1

        return patterns

    def _extract_subtree(self, root_key: str, component_def: Dict) -> Dict:
        """Extract a component and all its descendants"""
        subtree = {}

        def collect(key):
            if key not in component_def:
                return
            comp = component_def[key]
            subtree[key] = comp

            for child_key in comp.get("children", {}).keys():
                collect(child_key)

        collect(root_key)
        return subtree

    def _analyze_component_subtree(self, subtree: Dict, full_def: Dict) -> Dict:
        """Analyze a component subtree"""
        analysis = {
            "component_types": [],
            "has_form_elements": False,
            "has_buttons": False,
            "has_images": False,
            "has_text": False,
            "has_responsive": False,
            "has_semantic_names": False,
            "bindings": [],
            "events": [],
            "style_count": 0,
            "is_generic": True,
            "layout_type": None
        }

        semantic_name_patterns = [
            "header", "footer", "nav", "sidebar", "main", "content",
            "form", "input", "button", "card", "list", "item"
        ]

        for comp in subtree.values():
            comp_type = comp.get("type", "")
            comp_name = comp.get("name", "").lower()

            analysis["component_types"].append(comp_type)

            # Check for semantic names
            if any(p in comp_name for p in semantic_name_patterns):
                analysis["has_semantic_names"] = True

            # Track component types
            if comp_type in ["TextBox", "Dropdown", "Checkbox", "RadioButton"]:
                analysis["has_form_elements"] = True
                analysis["is_generic"] = False
            if comp_type == "Button":
                analysis["has_buttons"] = True
            if comp_type == "Image":
                analysis["has_images"] = True
            if comp_type == "Text":
                analysis["has_text"] = True

            # Track bindings
            if comp.get("bindingPath"):
                bp = comp["bindingPath"]
                if isinstance(bp, dict) and bp.get("value"):
                    analysis["bindings"].append(bp["value"])
                    analysis["is_generic"] = False

            # Track events
            props = comp.get("properties", {})
            for prop_name in ["onClick", "onEnter", "onChange", "onSubmit", "onLoad"]:
                if props.get(prop_name):
                    event_ref = props[prop_name]
                    if isinstance(event_ref, dict) and event_ref.get("value"):
                        analysis["events"].append(event_ref["value"])
                        analysis["is_generic"] = False

            # Count styles and check responsive
            style_props = comp.get("styleProperties", {})
            for style in style_props.values():
                resolutions = style.get("resolutions", {})
                for res_name, res_props in resolutions.items():
                    analysis["style_count"] += len(res_props)
                    if res_name != "ALL":
                        analysis["has_responsive"] = True

            # Detect layout type
            if props.get("layout"):
                layout = props["layout"]
                if isinstance(layout, dict):
                    analysis["layout_type"] = layout.get("value")
                else:
                    analysis["layout_type"] = layout
                analysis["is_generic"] = False

        return analysis

    def _generate_component_tags(self, analysis: Dict, source_info: Dict) -> List[str]:
        """Generate semantic tags for a component pattern"""
        tags = []

        # Add page type tag
        page_type = source_info.get("page_type", "other")
        if page_type != "other":
            tags.append(page_type)

        if analysis["has_form_elements"]:
            tags.append("form")
            if analysis["has_buttons"]:
                tags.append("form-with-submit")

        if analysis["has_images"] and analysis["has_text"]:
            tags.append("card")
            tags.append("content-block")

        if analysis["has_responsive"]:
            tags.append("responsive")

        if analysis["layout_type"]:
            layout = analysis["layout_type"]
            if "ROW" in layout:
                tags.append("horizontal-layout")
            if "COLUMN" in layout or "COL" in layout:
                tags.append("multi-column")
            tags.append(f"layout-{layout.lower()}")

        # Count component types
        type_counts = defaultdict(int)
        for t in analysis["component_types"]:
            type_counts[t] += 1

        if type_counts.get("Button", 0) > 3:
            tags.append("button-group")
        if type_counts.get("Text", 0) > 3:
            tags.append("text-heavy")
        if type_counts.get("Image", 0) > 2:
            tags.append("gallery")
        if type_counts.get("Link", 0) > 2:
            tags.append("link-list")

        if analysis["bindings"]:
            tags.append("data-bound")

        if analysis["events"]:
            tags.append("interactive")

        return list(set(tags))

    def _generate_component_description(self, analysis: Dict) -> str:
        """Generate description for component pattern"""
        parts = []

        type_counts = defaultdict(int)
        for t in analysis["component_types"]:
            type_counts[t] += 1

        main_types = sorted(type_counts.items(), key=lambda x: -x[1])[:3]
        parts.append(f"Contains: {', '.join(f'{c}x {t}' for t, c in main_types)}")

        if analysis["layout_type"]:
            parts.append(f"Layout: {analysis['layout_type']}")

        if analysis["has_responsive"]:
            parts.append("Responsive")

        if analysis["bindings"]:
            parts.append(f"Bound to: {', '.join(analysis['bindings'][:2])}")

        return ". ".join(parts)

    def _hash_component_tree(self, subtree: Dict, tags: List[str]) -> str:
        """Generate semantic hash for component tree"""
        # Hash based on structure
        structure = []

        for comp in subtree.values():
            structure.append(comp.get("type", ""))

        structure.sort()
        structure_str = "|".join(structure) + "|" + ",".join(sorted(tags))

        return hashlib.md5(structure_str.encode()).hexdigest()[:8]

    def _extract_form_patterns(
        self,
        component_def: Dict,
        event_functions: Dict,
        source_info: Dict
    ) -> List[ExtractedPattern]:
        """Extract complete form patterns"""
        patterns = []

        for comp_key, comp in component_def.items():
            children_keys = list(comp.get("children", {}).keys())

            form_elements = []
            submit_button = None

            for child_key in children_keys:
                child = component_def.get(child_key, {})
                child_type = child.get("type", "")

                if child_type in ["TextBox", "Dropdown", "Checkbox", "RadioButton"]:
                    form_elements.append(child_key)

                if child_type == "Button":
                    props = child.get("properties", {})
                    if props.get("onClick"):
                        submit_button = child_key

            if len(form_elements) >= 2 and submit_button:
                form_components = {comp_key: comp}
                for child_key in children_keys:
                    if child_key in component_def:
                        form_components[child_key] = component_def[child_key]

                # Find submit event
                submit_event = None
                submit_comp = component_def.get(submit_button, {})
                onclick = submit_comp.get("properties", {}).get("onClick", {})
                if isinstance(onclick, dict) and onclick.get("value"):
                    event_key = onclick["value"]
                    if event_key in event_functions:
                        submit_event = event_functions[event_key]

                # Extract bindings
                bindings = []
                for fe_key in form_elements:
                    fe = component_def.get(fe_key, {})
                    bp = fe.get("bindingPath", {})
                    if isinstance(bp, dict) and bp.get("value"):
                        bindings.append(bp["value"])

                # Determine form type
                form_type = "generic"
                binding_str = " ".join(bindings).lower()
                if "login" in binding_str or "password" in binding_str:
                    form_type = "login"
                elif "email" in binding_str and "name" in binding_str:
                    form_type = "contact"
                elif "search" in binding_str or "query" in binding_str:
                    form_type = "search"

                pattern = ExtractedPattern(
                    id=self._generate_id(f"form_{comp_key}_{source_info['page']}"),
                    type=PatternType.FORM_PATTERN,
                    name=f"Form: {comp.get('name', comp_key)}",
                    description=f"{form_type.capitalize()} form with {len(form_elements)} fields",
                    semantic_tags=["form", "input", "submit", f"{len(form_elements)}-fields", form_type],
                    semantic_category="form",
                    definition={
                        "components": form_components,
                        "formElementKeys": form_elements,
                        "submitButtonKey": submit_button,
                        "submitEvent": self._clean_event_definition(submit_event) if submit_event else None,
                        "formType": form_type
                    },
                    source_page=source_info["page"],
                    source_app=source_info["app"],
                    required_store_paths=bindings,
                    component_count=len(form_components),
                    quality_score=0.85
                )

                patterns.append(pattern)

        return patterns

    def _extract_layout_patterns(
        self,
        component_def: Dict,
        root_component: str,
        source_info: Dict
    ) -> List[ExtractedPattern]:
        """Extract page layout patterns"""
        patterns = []

        if not root_component or root_component not in component_def:
            return patterns

        root = component_def[root_component]
        children_keys = list(root.get("children", {}).keys())

        structure = []
        for child_key in children_keys:
            child = component_def.get(child_key, {})
            child_name = child.get("name", child_key).lower()

            # Detect semantic sections
            if any(x in child_name for x in ["header", "nav", "top"]):
                structure.append(("header", child_key))
            elif any(x in child_name for x in ["footer", "bottom"]):
                structure.append(("footer", child_key))
            elif any(x in child_name for x in ["side", "menu"]):
                structure.append(("sidebar", child_key))
            elif any(x in child_name for x in ["main", "content", "body"]):
                structure.append(("main", child_key))
            else:
                structure.append(("section", child_key))

        if len(structure) >= 2:
            skeleton = {root_component: self._create_skeleton(root)}
            for section_type, key in structure:
                if key in component_def:
                    skeleton[key] = self._create_skeleton(component_def[key])

            structure_desc = " + ".join(s[0] for s in structure)

            # Determine layout type
            has_sidebar = any(s[0] == "sidebar" for s in structure)
            has_header = any(s[0] == "header" for s in structure)
            has_footer = any(s[0] == "footer" for s in structure)

            layout_type = "basic"
            if has_sidebar and has_header:
                layout_type = "dashboard"
            elif has_header and has_footer:
                layout_type = "standard"
            elif has_sidebar:
                layout_type = "sidebar"

            pattern = ExtractedPattern(
                id=self._generate_id(f"layout_{source_info['page']}"),
                type=PatternType.LAYOUT_STRUCTURE,
                name=f"Layout: {structure_desc}",
                description=f"{layout_type.capitalize()} layout with {len(structure)} sections",
                semantic_tags=["layout", "page-structure", layout_type] + [s[0] for s in structure],
                semantic_category="layout",
                definition={
                    "rootComponent": root_component,
                    "structure": structure,
                    "skeleton": skeleton,
                    "layoutType": layout_type
                },
                source_page=source_info["page"],
                source_app=source_info["app"],
                component_count=len(structure) + 1,
                quality_score=0.8
            )

            patterns.append(pattern)

        return patterns

    def _create_skeleton(self, comp: Dict) -> Dict:
        """Create a skeleton version of a component"""
        skeleton = {
            "key": comp.get("key"),
            "name": comp.get("name"),
            "type": comp.get("type"),
        }

        if comp.get("properties", {}).get("layout"):
            skeleton["properties"] = {"layout": comp["properties"]["layout"]}

        if comp.get("children"):
            skeleton["children"] = comp["children"]

        return skeleton

    def _deduplicate(self, patterns: List[ExtractedPattern]) -> List[ExtractedPattern]:
        """Deduplicate patterns by semantic similarity"""
        unique = []

        for pattern in patterns:
            hash_key = f"{pattern.type.value}_{pattern.semantic_hash}"

            if hash_key not in self.seen_hashes:
                self.seen_hashes.add(hash_key)
                unique.append(pattern)
            else:
                # Keep the higher quality one
                for i, existing in enumerate(unique):
                    if existing.semantic_hash == pattern.semantic_hash:
                        if pattern.quality_score > existing.quality_score:
                            unique[i] = pattern
                        break

        return unique

    def _generate_id(self, base: str) -> str:
        """Generate a unique ID"""
        return hashlib.md5(base.encode()).hexdigest()[:12]

    def save_patterns(self, output_dir: str):
        """Save extracted patterns to files"""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        # Group patterns by type
        by_type = defaultdict(list)
        for pattern in self.patterns:
            by_type[pattern.type.value].append(pattern.to_dict())

        # Save each type
        for type_name, patterns in by_type.items():
            # Sort by quality score
            patterns.sort(key=lambda x: -x.get("quality_score", 0))

            file_path = output_path / f"{type_name}_patterns.json"
            with open(file_path, "w") as f:
                json.dump(patterns, f, indent=2)
            print(f"Saved {len(patterns)} {type_name} patterns to {file_path}")

            self.stats["by_type"][type_name] = len(patterns)

        # Save statistics
        stats_output = {
            "total_patterns": len(self.patterns),
            "pages_processed": self.stats["pages_processed"],
            "patterns_deduplicated": self.stats["patterns_deduplicated"],
            "by_type": dict(self.stats["by_type"]),
            "by_category": dict(self.stats["by_category"]),
            "event_functions_used": dict(sorted(
                self.stats["event_functions_used"].items(),
                key=lambda x: -x[1]
            )[:50]),
            "component_types_used": dict(sorted(
                self.stats["component_types_used"].items(),
                key=lambda x: -x[1]
            ))
        }

        with open(output_path / "summary.json", "w") as f:
            json.dump(stats_output, f, indent=2)

        print(f"\nTotal patterns extracted: {len(self.patterns)}")
        print(f"Patterns deduplicated: {self.stats['patterns_deduplicated']}")

    def generate_semantic_index(self, output_path: str):
        """Generate enhanced semantic index for RAG"""
        index = []

        for pattern in self.patterns:
            entry = {
                "id": pattern.id,
                "type": pattern.type.value,
                "name": pattern.name,
                "description": pattern.description,
                "tags": pattern.semantic_tags,
                "category": pattern.semantic_category,
                "quality_score": pattern.quality_score,
                "search_text": f"{pattern.name} {pattern.description} {' '.join(pattern.semantic_tags)} {pattern.semantic_category}",
                "complexity": "complex" if pattern.component_count > 10 or pattern.event_step_count > 5 else "simple",
                "component_count": pattern.component_count,
                "source": f"{pattern.source_app}/{pattern.source_page}"
            }
            index.append(entry)

        # Sort by quality
        index.sort(key=lambda x: -x["quality_score"])

        with open(output_path, "w") as f:
            json.dump(index, f, indent=2)

        print(f"Semantic index saved to {output_path}")


def extract_from_directory(definitions_dir: str, output_dir: str):
    """Extract patterns from all page definitions"""
    extractor = EnhancedPatternExtractor()

    definitions_path = Path(definitions_dir)

    # Find all page JSON files
    for page_file in definitions_path.rglob("*/Page/*.json"):
        try:
            with open(page_file) as f:
                page_def = json.load(f)

            parts = page_file.parts
            app_idx = parts.index("Page") - 1 if "Page" in parts else -2
            app_name = parts[app_idx] if app_idx >= 0 else "unknown"

            source_info = {
                "page": page_def.get("name", page_file.stem),
                "app": page_def.get("appCode", app_name),
                "file": str(page_file)
            }

            patterns = extractor.extract_from_page(page_def, source_info)
            print(f"Extracted {len(patterns)} patterns from {page_file.name}")

        except Exception as e:
            print(f"Error processing {page_file}: {e}")

    extractor.save_patterns(output_dir)
    extractor.generate_semantic_index(f"{output_dir}/semantic_index.json")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python pattern_extractor_v2.py <definitions_dir> <output_dir>")
        sys.exit(1)

    definitions_dir = sys.argv[1]
    output_dir = sys.argv[2]

    extract_from_directory(definitions_dir, output_dir)
