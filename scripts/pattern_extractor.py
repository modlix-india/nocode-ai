"""
Pattern Extractor for Nocode Page Definitions

This tool extracts reusable patterns from existing page definitions to create
a semantic knowledge base for AI page generation.

Categories of patterns:
1. COMPONENT PATTERNS - Reusable UI component subtrees
2. EVENT PATTERNS - KIRun event function patterns
3. STYLE PATTERNS - Responsive style configurations
4. LAYOUT PATTERNS - Page structure patterns
"""

import json
import os
import hashlib
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum
import re


class PatternType(Enum):
    COMPONENT_TREE = "component_tree"
    EVENT_FUNCTION = "event_function"
    STYLE_SET = "style_set"
    LAYOUT_STRUCTURE = "layout_structure"
    FORM_PATTERN = "form_pattern"
    DATA_BINDING = "data_binding"


@dataclass
class ExtractedPattern:
    """A single extracted pattern"""
    id: str
    type: PatternType
    name: str
    description: str
    semantic_tags: List[str]  # For searching: ["login", "form", "authentication"]

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

    # Complexity metrics
    component_count: int = 0
    event_step_count: int = 0
    style_property_count: int = 0

    def to_dict(self) -> Dict:
        d = asdict(self)
        d['type'] = self.type.value
        return d


class PatternExtractor:
    """Extracts patterns from page definitions"""

    def __init__(self):
        self.patterns: List[ExtractedPattern] = []
        self.component_type_stats: Dict[str, int] = {}
        self.event_function_stats: Dict[str, int] = {}

    def extract_from_page(self, page_def: Dict, source_info: Dict) -> List[ExtractedPattern]:
        """Extract all patterns from a page definition"""
        patterns = []

        # 1. Extract event function patterns
        event_patterns = self._extract_event_patterns(
            page_def.get("eventFunctions", {}),
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

        # 3. Extract form patterns (components with bindingPath + validation)
        form_patterns = self._extract_form_patterns(
            page_def.get("componentDefinition", {}),
            page_def.get("eventFunctions", {}),
            source_info
        )
        patterns.extend(form_patterns)

        # 4. Extract layout patterns
        layout_patterns = self._extract_layout_patterns(
            page_def.get("componentDefinition", {}),
            page_def.get("rootComponent", ""),
            source_info
        )
        patterns.extend(layout_patterns)

        self.patterns.extend(patterns)
        return patterns

    def _extract_event_patterns(
        self,
        event_functions: Dict[str, Any],
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

            # Analyze the event to understand what it does
            analysis = self._analyze_event_function(event_def)

            # Generate semantic tags based on analysis
            tags = self._generate_event_tags(analysis)

            # Create a cleaned version of the event (remove position data, simplify keys)
            cleaned_def = self._clean_event_definition(event_def)

            pattern = ExtractedPattern(
                id=self._generate_id(f"event_{event_key}_{source_info['page']}"),
                type=PatternType.EVENT_FUNCTION,
                name=event_def.get("name", event_key),
                description=self._generate_event_description(analysis),
                semantic_tags=tags,
                definition=cleaned_def,
                source_page=source_info["page"],
                source_app=source_info["app"],
                required_store_paths=analysis.get("reads_from", []),
                produced_store_paths=analysis.get("writes_to", []),
                event_step_count=len(steps)
            )

            patterns.append(pattern)

            # Track stats
            for step in steps.values():
                func_name = step.get("name", "unknown")
                self.event_function_stats[func_name] = \
                    self.event_function_stats.get(func_name, 0) + 1

        return patterns

    def _analyze_event_function(self, event_def: Dict) -> Dict:
        """Deeply analyze an event function to understand its behavior"""
        analysis = {
            "functions_used": [],
            "has_api_call": False,
            "has_conditional": False,
            "has_navigation": False,
            "has_validation": False,
            "reads_from": [],
            "writes_to": [],
            "api_endpoints": [],
            "dependency_chain": [],
            "complexity": "simple"
        }

        steps = event_def.get("steps", {})

        # Build dependency graph
        dep_graph = {}
        for step_key, step in steps.items():
            deps = step.get("dependentStatements", {})
            dep_graph[step_key] = list(deps.keys()) if deps else []

        analysis["dependency_chain"] = self._topological_sort(dep_graph)

        for step_key, step in steps.items():
            func_name = step.get("name", "")
            namespace = step.get("namespace", "")

            analysis["functions_used"].append(f"{namespace}.{func_name}")

            # Detect patterns
            if func_name in ["SendData", "FetchData"]:
                analysis["has_api_call"] = True
                # Extract endpoint
                url_param = step.get("parameterMap", {}).get("url", {})
                for param in url_param.values():
                    if param.get("value"):
                        analysis["api_endpoints"].append(param["value"])

            if func_name == "If":
                analysis["has_conditional"] = True

            if func_name == "Navigate":
                analysis["has_navigation"] = True

            if func_name == "Login":
                analysis["has_validation"] = True
                analysis["semantic_tags"] = ["authentication", "login"]

            # Track store access
            param_map = step.get("parameterMap", {})

            # SetStore writes
            if func_name == "SetStore":
                path_param = param_map.get("path", {})
                for p in path_param.values():
                    if p.get("value"):
                        analysis["writes_to"].append(p["value"])

            # Extract expressions for reads
            self._extract_store_reads(param_map, analysis["reads_from"])

        # Determine complexity
        if len(steps) > 5 or analysis["has_conditional"]:
            analysis["complexity"] = "complex"
        elif len(steps) > 2 or analysis["has_api_call"]:
            analysis["complexity"] = "moderate"

        return analysis

    def _extract_store_reads(self, param_map: Dict, reads_list: List[str]):
        """Extract all store paths that are read in expressions"""
        store_pattern = re.compile(r'(Page\.[a-zA-Z0-9_.]+|Store\.[a-zA-Z0-9_.]+|Steps\.[a-zA-Z0-9_.]+)')

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

    def _generate_event_tags(self, analysis: Dict) -> List[str]:
        """Generate semantic tags for an event based on analysis"""
        tags = []

        if analysis["has_api_call"]:
            tags.append("api")
            tags.append("async")
            for endpoint in analysis["api_endpoints"]:
                if "login" in endpoint.lower():
                    tags.extend(["login", "authentication"])
                elif "user" in endpoint.lower():
                    tags.append("user-management")
                elif any(x in endpoint.lower() for x in ["save", "create", "update"]):
                    tags.append("data-mutation")
                elif any(x in endpoint.lower() for x in ["get", "fetch", "list"]):
                    tags.append("data-fetch")

        if analysis["has_conditional"]:
            tags.append("conditional")
            tags.append("branching")

        if analysis["has_navigation"]:
            tags.append("navigation")
            tags.append("routing")

        # Detect calculator pattern
        writes = analysis["writes_to"]
        if any("display" in w.lower() for w in writes):
            tags.append("calculator")
            tags.append("display-update")

        # Detect form pattern
        if any("form" in w.lower() for w in writes):
            tags.append("form")

        # Detect toggle pattern
        funcs = analysis["functions_used"]
        if len(funcs) == 1 and "SetStore" in funcs[0]:
            if len(writes) == 1:
                tags.append("simple-update")
                tags.append("toggle")

        tags.append(analysis["complexity"])

        return list(set(tags))

    def _generate_event_description(self, analysis: Dict) -> str:
        """Generate a human-readable description of the event"""
        parts = []

        if analysis["has_api_call"]:
            endpoints = analysis["api_endpoints"]
            if endpoints:
                parts.append(f"Calls API: {endpoints[0]}")
            else:
                parts.append("Makes API call")

        if analysis["has_conditional"]:
            parts.append("Has conditional branching")

        if analysis["has_navigation"]:
            parts.append("Navigates to another page")

        writes = analysis["writes_to"]
        if writes:
            parts.append(f"Updates: {', '.join(writes[:3])}")

        if not parts:
            parts.append("Simple store update")

        return ". ".join(parts)

    def _clean_event_definition(self, event_def: Dict) -> Dict:
        """Clean an event definition for storage (remove UI-specific data)"""
        cleaned = {
            "name": event_def.get("name"),
            "namespace": event_def.get("namespace", ""),
            "steps": {}
        }

        if event_def.get("validationCheck"):
            cleaned["validationCheck"] = "__VALIDATION_COMPONENT__"

        for step_key, step in event_def.get("steps", {}).items():
            cleaned_step = {
                "statementName": step.get("statementName"),
                "name": step.get("name"),
                "namespace": step.get("namespace"),
            }

            # Keep parameterMap but with simplified keys
            if "parameterMap" in step:
                cleaned_step["parameterMap"] = self._simplify_param_map(
                    step["parameterMap"]
                )

            # Keep dependencies
            if "dependentStatements" in step:
                cleaned_step["dependentStatements"] = step["dependentStatements"]

            cleaned["steps"][step_key] = cleaned_step

        return cleaned

    def _simplify_param_map(self, param_map: Dict) -> Dict:
        """Simplify parameter map keys to 'one', 'two', etc."""
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

    def _extract_component_patterns(
        self,
        component_def: Dict[str, Any],
        root_component: str,
        source_info: Dict
    ) -> List[ExtractedPattern]:
        """Extract component subtree patterns"""
        patterns = []

        # Find interesting subtrees (not just single components)
        for comp_key, comp in component_def.items():
            children = comp.get("children", {})

            # Skip leaf components
            if not children:
                continue

            # Skip if too simple (just 1-2 children with no special properties)
            if len(children) < 2:
                continue

            # Analyze the subtree
            subtree = self._extract_subtree(comp_key, component_def)
            analysis = self._analyze_component_subtree(subtree, component_def)

            # Skip generic grids with no semantic meaning
            if analysis["is_generic"]:
                continue

            # Create pattern
            tags = self._generate_component_tags(analysis)

            pattern = ExtractedPattern(
                id=self._generate_id(f"comp_{comp_key}_{source_info['page']}"),
                type=PatternType.COMPONENT_TREE,
                name=comp.get("name", comp_key),
                description=self._generate_component_description(analysis),
                semantic_tags=tags,
                definition={"rootKey": comp_key, "components": subtree},
                source_page=source_info["page"],
                source_app=source_info["app"],
                source_component_key=comp_key,
                required_store_paths=analysis.get("bindings", []),
                referenced_events=analysis.get("events", []),
                component_count=len(subtree),
                style_property_count=analysis.get("style_count", 0)
            )

            patterns.append(pattern)

        return patterns

    def _extract_subtree(
        self,
        root_key: str,
        component_def: Dict
    ) -> Dict[str, Any]:
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

    def _analyze_component_subtree(
        self,
        subtree: Dict,
        full_def: Dict
    ) -> Dict:
        """Analyze a component subtree"""
        analysis = {
            "component_types": [],
            "has_form_elements": False,
            "has_buttons": False,
            "has_images": False,
            "has_text": False,
            "bindings": [],
            "events": [],
            "style_count": 0,
            "is_generic": True,
            "layout_type": None
        }

        for comp in subtree.values():
            comp_type = comp.get("type", "")
            analysis["component_types"].append(comp_type)

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
            for prop_name in ["onClick", "onEnter", "onChange", "onSubmit"]:
                if props.get(prop_name):
                    event_ref = props[prop_name]
                    if isinstance(event_ref, dict) and event_ref.get("value"):
                        analysis["events"].append(event_ref["value"])
                        analysis["is_generic"] = False

            # Count styles
            style_props = comp.get("styleProperties", {})
            for style in style_props.values():
                resolutions = style.get("resolutions", {})
                for res_props in resolutions.values():
                    analysis["style_count"] += len(res_props)

            # Detect layout type
            if props.get("layout"):
                layout = props["layout"]
                if isinstance(layout, dict):
                    analysis["layout_type"] = layout.get("value")
                else:
                    analysis["layout_type"] = layout

        return analysis

    def _generate_component_tags(self, analysis: Dict) -> List[str]:
        """Generate semantic tags for a component pattern"""
        tags = []

        if analysis["has_form_elements"]:
            tags.append("form")
            if analysis["has_buttons"]:
                tags.append("form-with-submit")

        if analysis["has_images"] and analysis["has_text"]:
            tags.append("card")
            tags.append("content-block")

        if analysis["layout_type"]:
            layout = analysis["layout_type"]
            if "ROW" in layout:
                tags.append("horizontal-layout")
            if "COLUMN" in layout or "COL" in layout:
                tags.append("multi-column")
            tags.append(f"layout-{layout.lower()}")

        # Count component types
        type_counts = {}
        for t in analysis["component_types"]:
            type_counts[t] = type_counts.get(t, 0) + 1

        if type_counts.get("Button", 0) > 3:
            tags.append("button-group")
        if type_counts.get("Text", 0) > 3:
            tags.append("text-heavy")
        if type_counts.get("Image", 0) > 2:
            tags.append("gallery")

        if analysis["bindings"]:
            tags.append("data-bound")

        return tags

    def _generate_component_description(self, analysis: Dict) -> str:
        """Generate description for component pattern"""
        parts = []

        type_counts = {}
        for t in analysis["component_types"]:
            type_counts[t] = type_counts.get(t, 0) + 1

        main_types = sorted(type_counts.items(), key=lambda x: -x[1])[:3]
        parts.append(f"Contains: {', '.join(f'{c}x {t}' for t, c in main_types)}")

        if analysis["layout_type"]:
            parts.append(f"Layout: {analysis['layout_type']}")

        if analysis["bindings"]:
            parts.append(f"Bound to: {', '.join(analysis['bindings'][:2])}")

        return ". ".join(parts)

    def _extract_form_patterns(
        self,
        component_def: Dict,
        event_functions: Dict,
        source_info: Dict
    ) -> List[ExtractedPattern]:
        """Extract complete form patterns (components + validation + submit event)"""
        patterns = []

        # Find form containers (Grid with form inputs as children)
        for comp_key, comp in component_def.items():
            children_keys = list(comp.get("children", {}).keys())

            # Check if this grid contains form elements
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

            # If we have form elements and a submit button, this is a form pattern
            if len(form_elements) >= 2 and submit_button:
                # Extract the complete form
                form_components = {comp_key: comp}
                for child_key in children_keys:
                    if child_key in component_def:
                        form_components[child_key] = component_def[child_key]

                # Find the submit event
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

                pattern = ExtractedPattern(
                    id=self._generate_id(f"form_{comp_key}_{source_info['page']}"),
                    type=PatternType.FORM_PATTERN,
                    name=f"Form: {comp.get('name', comp_key)}",
                    description=f"Form with {len(form_elements)} fields and submit button",
                    semantic_tags=["form", "input", "submit", f"{len(form_elements)}-fields"],
                    definition={
                        "components": form_components,
                        "formElementKeys": form_elements,
                        "submitButtonKey": submit_button,
                        "submitEvent": self._clean_event_definition(submit_event) if submit_event else None
                    },
                    source_page=source_info["page"],
                    source_app=source_info["app"],
                    required_store_paths=bindings,
                    component_count=len(form_components)
                )

                patterns.append(pattern)

        return patterns

    def _extract_layout_patterns(
        self,
        component_def: Dict,
        root_component: str,
        source_info: Dict
    ) -> List[ExtractedPattern]:
        """Extract page layout patterns (header/main/footer structure)"""
        patterns = []

        if not root_component or root_component not in component_def:
            return patterns

        root = component_def[root_component]
        children_keys = list(root.get("children", {}).keys())

        # Analyze top-level structure
        structure = []
        for child_key in children_keys:
            child = component_def.get(child_key, {})
            child_name = child.get("name", child_key).lower()
            child_type = child.get("type", "")

            # Detect semantic sections
            if any(x in child_name for x in ["header", "nav", "top"]):
                structure.append(("header", child_key))
            elif any(x in child_name for x in ["footer", "bottom"]):
                structure.append(("footer", child_key))
            elif any(x in child_name for x in ["side", "menu", "nav"]):
                structure.append(("sidebar", child_key))
            elif any(x in child_name for x in ["main", "content", "body"]):
                structure.append(("main", child_key))
            else:
                structure.append(("section", child_key))

        if len(structure) >= 2:
            # Create skeleton layout (just structure, no content)
            skeleton = {root_component: self._create_skeleton(root)}
            for section_type, key in structure:
                if key in component_def:
                    skeleton[key] = self._create_skeleton(component_def[key])

            structure_desc = " + ".join(s[0] for s in structure)

            pattern = ExtractedPattern(
                id=self._generate_id(f"layout_{source_info['page']}"),
                type=PatternType.LAYOUT_STRUCTURE,
                name=f"Layout: {structure_desc}",
                description=f"Page structure with {len(structure)} sections: {structure_desc}",
                semantic_tags=["layout", "page-structure"] + [s[0] for s in structure],
                definition={
                    "rootComponent": root_component,
                    "structure": structure,
                    "skeleton": skeleton
                },
                source_page=source_info["page"],
                source_app=source_info["app"],
                component_count=len(structure) + 1
            )

            patterns.append(pattern)

        return patterns

    def _create_skeleton(self, comp: Dict) -> Dict:
        """Create a skeleton version of a component (structure only)"""
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

    def _topological_sort(self, graph: Dict[str, List[str]]) -> List[str]:
        """Sort steps by dependency order"""
        visited = set()
        result = []

        def visit(node):
            if node in visited:
                return
            visited.add(node)
            for dep in graph.get(node, []):
                # Extract step name from "Steps.stepName.output"
                parts = dep.split(".")
                if len(parts) >= 2:
                    step_name = parts[1]
                    visit(step_name)
            result.append(node)

        for node in graph:
            visit(node)

        return result

    def _generate_id(self, base: str) -> str:
        """Generate a unique ID for a pattern"""
        return hashlib.md5(base.encode()).hexdigest()[:12]

    def save_patterns(self, output_dir: str):
        """Save extracted patterns to files"""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        # Group patterns by type
        by_type = {}
        for pattern in self.patterns:
            type_name = pattern.type.value
            if type_name not in by_type:
                by_type[type_name] = []
            by_type[type_name].append(pattern.to_dict())

        # Save each type to a separate file
        for type_name, patterns in by_type.items():
            file_path = output_path / f"{type_name}_patterns.json"
            with open(file_path, "w") as f:
                json.dump(patterns, f, indent=2)
            print(f"Saved {len(patterns)} {type_name} patterns to {file_path}")

        # Save summary
        summary = {
            "total_patterns": len(self.patterns),
            "by_type": {k: len(v) for k, v in by_type.items()},
            "component_type_stats": self.component_type_stats,
            "event_function_stats": self.event_function_stats
        }

        with open(output_path / "summary.json", "w") as f:
            json.dump(summary, f, indent=2)

        print(f"\nTotal patterns extracted: {len(self.patterns)}")
        print(f"Summary saved to {output_path / 'summary.json'}")

    def generate_semantic_index(self, output_path: str):
        """Generate a semantic index for RAG retrieval"""
        index = []

        for pattern in self.patterns:
            entry = {
                "id": pattern.id,
                "type": pattern.type.value,
                "name": pattern.name,
                "description": pattern.description,
                "tags": pattern.semantic_tags,
                "search_text": f"{pattern.name} {pattern.description} {' '.join(pattern.semantic_tags)}",
                "complexity": "complex" if pattern.component_count > 10 or pattern.event_step_count > 5 else "simple"
            }
            index.append(entry)

        with open(output_path, "w") as f:
            json.dump(index, f, indent=2)

        print(f"Semantic index saved to {output_path}")


def extract_from_directory(definitions_dir: str, output_dir: str):
    """Extract patterns from all page definitions in a directory"""
    extractor = PatternExtractor()

    definitions_path = Path(definitions_dir)

    # Find all page JSON files
    for page_file in definitions_path.rglob("*/Page/*.json"):
        try:
            with open(page_file) as f:
                page_def = json.load(f)

            # Extract source info from path
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

    # Save results
    extractor.save_patterns(output_dir)
    extractor.generate_semantic_index(f"{output_dir}/semantic_index.json")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python pattern_extractor.py <definitions_dir> <output_dir>")
        print("Example: python pattern_extractor.py ./definitions ./extracted_patterns")
        sys.exit(1)

    definitions_dir = sys.argv[1]
    output_dir = sys.argv[2]

    extract_from_directory(definitions_dir, output_dir)
