"""
Pattern Extractor V3 - Functional Patterns

This version extracts COMPLETE functional patterns that include:
1. Component UI structure
2. Event functions that components use
3. Data bindings and their relationships
4. API call patterns
5. State management patterns

The goal is to extract patterns that actually WORK, not just render.

Usage:
    python pattern_extractor_v3.py --input-dir ./definitions --output-dir ./extracted_patterns_v3
"""

import json
import os
import sys
import argparse
import hashlib
from pathlib import Path
from typing import Dict, Any, List, Optional, Set, Tuple
from dataclasses import dataclass, asdict, field
from enum import Enum
from collections import defaultdict


class FunctionalPatternType(Enum):
    """Types of functional patterns we extract"""
    LOGIN_FORM = "login_form"              # Login with authentication
    SIGNUP_FORM = "signup_form"            # Registration form
    CONTACT_FORM = "contact_form"          # Contact/inquiry form
    DATA_TABLE = "data_table"              # Table with CRUD operations
    SEARCH_FILTER = "search_filter"        # Search with filtering
    MODAL_CONFIRM = "modal_confirm"        # Confirmation modal
    NAVIGATION = "navigation"              # Navigation with routing
    CRUD_FORM = "crud_form"                # Create/Update form with API
    DATA_LIST = "data_list"                # List with data fetching
    CALCULATOR = "calculator"              # Calculator with logic
    FILE_UPLOAD = "file_upload"            # File upload with handling
    STEP_WIZARD = "step_wizard"            # Multi-step wizard
    GENERIC = "generic"                    # Unclassified but functional


@dataclass
class ComponentEventBinding:
    """Represents the relationship between a component and its events/data"""
    component_key: str
    component_type: str
    component_name: str
    binding_path: Optional[str] = None
    events: Dict[str, str] = field(default_factory=dict)  # event_name -> event_function_name
    properties: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EventFunctionInfo:
    """Information about an event function"""
    name: str
    steps: List[str]  # List of step names (e.g., SetStore, FetchData)
    api_calls: List[Dict[str, Any]] = field(default_factory=list)  # API endpoints called
    state_mutations: List[str] = field(default_factory=list)  # Paths modified
    dependencies: List[str] = field(default_factory=list)  # Other events/steps depended on
    conditions: List[str] = field(default_factory=list)  # Conditions used


@dataclass
class FunctionalPattern:
    """A complete functional pattern with UI + logic + data"""
    id: str
    type: FunctionalPatternType
    name: str
    description: str
    source_page: str

    # UI Structure
    root_component: str
    components: Dict[str, Any]

    # Logic
    event_functions: Dict[str, Any]

    # Data flow
    bindings: List[ComponentEventBinding]
    state_paths: Set[str]  # All Page.xxx, Store.xxx paths used

    # API integration
    api_endpoints: List[Dict[str, str]]  # url, method

    # Metadata
    component_count: int
    event_count: int
    has_api_calls: bool
    has_conditional_logic: bool
    complexity_score: float

    # Semantic tags for search
    semantic_tags: List[str] = field(default_factory=list)


class FunctionalPatternExtractor:
    """Extracts complete functional patterns from pages"""

    # Patterns to detect functionality
    FORM_INDICATORS = {'TextBox', 'TextArea', 'Dropdown', 'CheckBox', 'RadioButton', 'PhoneNumber', 'Otp'}
    SUBMIT_EVENTS = {'onClick', 'onSubmit', 'onButtonClick'}
    API_FUNCTIONS = {'SendData', 'FetchData', 'DeleteData'}
    STATE_FUNCTIONS = {'SetStore'}
    NAVIGATION_FUNCTIONS = {'Navigate', 'NavigateTo'}

    def __init__(self):
        self.patterns: List[FunctionalPattern] = []

    def extract_from_page(self, page: Dict[str, Any], source_path: str) -> List[FunctionalPattern]:
        """Extract all functional patterns from a page"""
        patterns = []

        page_name = page.get('name', 'unknown')
        components = page.get('componentDefinition', {})
        events = page.get('eventFunctions', {})
        root = page.get('rootComponent', '')

        if not components or not root:
            return []

        # Analyze component-event bindings
        bindings = self._analyze_bindings(components, events)

        # Analyze event functions
        event_info = self._analyze_events(events)

        # Find all state paths used
        state_paths = self._find_state_paths(components, events)

        # Find API endpoints
        api_endpoints = self._find_api_endpoints(events)

        # Detect pattern type
        pattern_type = self._detect_pattern_type(components, events, bindings)

        # Extract sub-patterns if the page is complex
        if len(components) > 50:
            # For large pages, extract sub-sections
            sub_patterns = self._extract_sub_patterns(page, bindings, event_info)
            patterns.extend(sub_patterns)
        else:
            # Extract as single pattern
            pattern = self._create_pattern(
                page_name=page_name,
                source_path=source_path,
                root_component=root,
                components=components,
                events=events,
                bindings=bindings,
                state_paths=state_paths,
                api_endpoints=api_endpoints,
                pattern_type=pattern_type
            )
            if pattern:
                patterns.append(pattern)

        return patterns

    def _analyze_bindings(
        self,
        components: Dict[str, Any],
        events: Dict[str, Any]
    ) -> List[ComponentEventBinding]:
        """Analyze how components bind to data and events"""
        bindings = []

        for comp_key, comp in components.items():
            binding_path = comp.get('bindingPath', {}).get('value')
            comp_events = comp.get('events', {})

            if binding_path or comp_events:
                binding = ComponentEventBinding(
                    component_key=comp_key,
                    component_type=comp.get('type', 'unknown'),
                    component_name=comp.get('name', 'unnamed'),
                    binding_path=binding_path,
                    events=comp_events,
                    properties=comp.get('properties', {})
                )
                bindings.append(binding)

        return bindings

    def _analyze_events(self, events: Dict[str, Any]) -> Dict[str, EventFunctionInfo]:
        """Analyze event functions for their behavior"""
        event_info = {}

        for event_name, event_def in events.items():
            steps = event_def.get('steps', {})
            step_names = []
            api_calls = []
            state_mutations = []
            dependencies = []
            conditions = []

            for step_name, step in steps.items():
                fn_name = step.get('name', '')
                step_names.append(fn_name)

                # Check for API calls
                if fn_name in ('SendData', 'FetchData', 'DeleteData'):
                    param_map = step.get('parameterMap', {})
                    url_param = param_map.get('url', {})
                    method_param = param_map.get('method', {})

                    url = self._extract_param_value(url_param)
                    method = self._extract_param_value(method_param) or 'GET'

                    if url:
                        api_calls.append({
                            'url': url,
                            'method': method,
                            'step': step_name
                        })

                # Check for state mutations
                if fn_name == 'SetStore':
                    param_map = step.get('parameterMap', {})
                    path_param = param_map.get('path', {})
                    path = self._extract_param_value(path_param)
                    if path:
                        state_mutations.append(path)

                # Check for conditions
                if fn_name == 'If':
                    param_map = step.get('parameterMap', {})
                    condition_param = param_map.get('condition', {})
                    condition = self._extract_param_value(condition_param)
                    if condition:
                        conditions.append(condition)

                # Check dependencies
                deps = step.get('dependentStatements', {})
                if deps:
                    dependencies.extend(list(deps.keys()))

            event_info[event_name] = EventFunctionInfo(
                name=event_name,
                steps=step_names,
                api_calls=api_calls,
                state_mutations=state_mutations,
                dependencies=dependencies,
                conditions=conditions
            )

        return event_info

    def _extract_param_value(self, param: Dict) -> Optional[str]:
        """Extract the actual value from a parameter map"""
        for key, val in param.items():
            if isinstance(val, dict):
                if val.get('type') == 'VALUE':
                    return val.get('value')
                elif val.get('type') == 'EXPRESSION':
                    return val.get('expression')
        return None

    def _find_state_paths(
        self,
        components: Dict[str, Any],
        events: Dict[str, Any]
    ) -> Set[str]:
        """Find all state paths (Page.xxx, Store.xxx) used"""
        paths = set()

        # From component bindings
        for comp in components.values():
            binding = comp.get('bindingPath', {}).get('value')
            if binding:
                paths.add(binding)

        # From events
        for event in events.values():
            for step in event.get('steps', {}).values():
                param_map = step.get('parameterMap', {})
                for param_name, param_values in param_map.items():
                    for val in param_values.values():
                        if isinstance(val, dict):
                            expr = val.get('expression', '') or val.get('value', '')
                            if expr:
                                # Extract Page.xxx and Store.xxx references
                                for prefix in ('Page.', 'Store.', 'Parent.'):
                                    if prefix in str(expr):
                                        # Simple extraction
                                        parts = str(expr).split()
                                        for part in parts:
                                            if part.startswith(prefix):
                                                # Clean up
                                                path = part.split('[')[0].split('(')[0]
                                                paths.add(path)

        return paths

    def _find_api_endpoints(self, events: Dict[str, Any]) -> List[Dict[str, str]]:
        """Find all API endpoints called"""
        endpoints = []

        for event in events.values():
            for step in event.get('steps', {}).values():
                fn_name = step.get('name', '')
                if fn_name in ('SendData', 'FetchData', 'DeleteData'):
                    param_map = step.get('parameterMap', {})
                    url = self._extract_param_value(param_map.get('url', {}))
                    method = self._extract_param_value(param_map.get('method', {})) or 'GET'
                    if url:
                        endpoints.append({'url': url, 'method': method})

        return endpoints

    def _detect_pattern_type(
        self,
        components: Dict[str, Any],
        events: Dict[str, Any],
        bindings: List[ComponentEventBinding]
    ) -> FunctionalPatternType:
        """Detect the type of functional pattern"""

        # Collect indicators
        comp_types = set(c.get('type', '') for c in components.values())
        comp_names = ' '.join(c.get('name', '').lower() for c in components.values())

        has_form_inputs = bool(comp_types & self.FORM_INDICATORS)
        has_table = 'Table' in comp_types or 'TableGrid' in comp_types
        has_popup = 'Popup' in comp_types
        has_repeater = 'ArrayRepeater' in comp_types

        # Check event patterns
        has_login = False
        has_api_calls = False
        has_navigation = False

        for event in events.values():
            for step in event.get('steps', {}).values():
                fn_name = step.get('name', '')
                if fn_name == 'Login' or fn_name == 'LOGIN':
                    has_login = True
                if fn_name in self.API_FUNCTIONS:
                    has_api_calls = True
                if fn_name in self.NAVIGATION_FUNCTIONS:
                    has_navigation = True

        # Detect type
        if has_login or 'login' in comp_names:
            return FunctionalPatternType.LOGIN_FORM
        elif 'signup' in comp_names or 'register' in comp_names:
            return FunctionalPatternType.SIGNUP_FORM
        elif 'contact' in comp_names or 'enquiry' in comp_names or 'inquiry' in comp_names:
            return FunctionalPatternType.CONTACT_FORM
        elif has_table and has_api_calls:
            return FunctionalPatternType.DATA_TABLE
        elif has_popup and len(events) >= 2:
            return FunctionalPatternType.MODAL_CONFIRM
        elif has_repeater and has_api_calls:
            return FunctionalPatternType.DATA_LIST
        elif has_form_inputs and has_api_calls:
            return FunctionalPatternType.CRUD_FORM
        elif has_navigation:
            return FunctionalPatternType.NAVIGATION
        elif 'calculator' in comp_names or 'calc' in comp_names:
            return FunctionalPatternType.CALCULATOR
        elif 'upload' in comp_names or 'FileUpload' in comp_types:
            return FunctionalPatternType.FILE_UPLOAD
        elif 'step' in comp_names or 'wizard' in comp_names or 'Stepper' in comp_types:
            return FunctionalPatternType.STEP_WIZARD
        else:
            return FunctionalPatternType.GENERIC

    def _create_pattern(
        self,
        page_name: str,
        source_path: str,
        root_component: str,
        components: Dict[str, Any],
        events: Dict[str, Any],
        bindings: List[ComponentEventBinding],
        state_paths: Set[str],
        api_endpoints: List[Dict[str, str]],
        pattern_type: FunctionalPatternType
    ) -> Optional[FunctionalPattern]:
        """Create a functional pattern from analyzed data"""

        # Skip if no functional elements
        if not events and not bindings:
            return None

        # Generate ID
        content_hash = hashlib.md5(
            json.dumps({
                'components': list(components.keys())[:10],
                'events': list(events.keys()),
                'type': pattern_type.value
            }, sort_keys=True).encode()
        ).hexdigest()[:12]

        # Calculate complexity
        complexity = (
            len(components) * 0.1 +
            len(events) * 0.5 +
            len(api_endpoints) * 1.0 +
            len(state_paths) * 0.3
        )

        # Generate semantic tags
        tags = self._generate_tags(components, events, pattern_type)

        # Build description
        comp_types = defaultdict(int)
        for comp in components.values():
            comp_types[comp.get('type', 'unknown')] += 1

        desc_parts = [f"{pattern_type.value.replace('_', ' ').title()}"]
        if api_endpoints:
            desc_parts.append(f"with {len(api_endpoints)} API calls")
        if state_paths:
            desc_parts.append(f"{len(state_paths)} state bindings")

        return FunctionalPattern(
            id=content_hash,
            type=pattern_type,
            name=page_name,
            description=' '.join(desc_parts),
            source_page=source_path,
            root_component=root_component,
            components=components,
            event_functions=events,
            bindings=bindings,
            state_paths=state_paths,
            api_endpoints=api_endpoints,
            component_count=len(components),
            event_count=len(events),
            has_api_calls=len(api_endpoints) > 0,
            has_conditional_logic=any(
                'If' in str(e.get('steps', {}))
                for e in events.values()
            ),
            complexity_score=complexity,
            semantic_tags=tags
        )

    def _generate_tags(
        self,
        components: Dict[str, Any],
        events: Dict[str, Any],
        pattern_type: FunctionalPatternType
    ) -> List[str]:
        """Generate semantic tags for the pattern"""
        tags = [pattern_type.value]

        # Component-based tags
        comp_types = set(c.get('type', '') for c in components.values())
        if 'TextBox' in comp_types:
            tags.append('input')
        if 'Button' in comp_types:
            tags.append('interactive')
        if 'Table' in comp_types or 'TableGrid' in comp_types:
            tags.append('tabular')
        if 'ArrayRepeater' in comp_types:
            tags.append('list')
        if 'Popup' in comp_types:
            tags.append('modal')
        if 'Image' in comp_types:
            tags.append('visual')

        # Event-based tags
        for event in events.values():
            for step in event.get('steps', {}).values():
                fn_name = step.get('name', '')
                if fn_name in ('SendData', 'FetchData'):
                    tags.append('api-integration')
                if fn_name == 'SetStore':
                    tags.append('stateful')
                if fn_name == 'Navigate':
                    tags.append('routing')
                if fn_name == 'Message':
                    tags.append('user-feedback')

        return list(set(tags))

    def _extract_sub_patterns(
        self,
        page: Dict[str, Any],
        bindings: List[ComponentEventBinding],
        event_info: Dict[str, EventFunctionInfo]
    ) -> List[FunctionalPattern]:
        """Extract sub-patterns from a large page"""
        patterns = []
        components = page.get('componentDefinition', {})
        events = page.get('eventFunctions', {})

        # Group components by their event bindings
        event_to_components: Dict[str, List[str]] = defaultdict(list)

        for binding in bindings:
            for event_name in binding.events.values():
                event_to_components[event_name].append(binding.component_key)

        # Extract each functional group
        for event_name, comp_keys in event_to_components.items():
            if event_name not in events:
                continue

            # Get the event and related components
            event_def = events[event_name]
            related_comps = {}

            # Get the components and their children
            for comp_key in comp_keys:
                if comp_key in components:
                    self._collect_component_tree(components, comp_key, related_comps)

            if len(related_comps) < 2:
                continue

            # Find root of this sub-tree
            root = self._find_subtree_root(related_comps)

            # Create sub-pattern
            sub_bindings = [b for b in bindings if b.component_key in related_comps]
            state_paths = self._find_state_paths(related_comps, {event_name: event_def})
            api_endpoints = self._find_api_endpoints({event_name: event_def})
            pattern_type = self._detect_pattern_type(
                related_comps,
                {event_name: event_def},
                sub_bindings
            )

            pattern = self._create_pattern(
                page_name=f"{page.get('name', 'page')}_{event_name}",
                source_path=page.get('name', 'unknown'),
                root_component=root,
                components=related_comps,
                events={event_name: event_def},
                bindings=sub_bindings,
                state_paths=state_paths,
                api_endpoints=api_endpoints,
                pattern_type=pattern_type
            )

            if pattern and pattern.component_count >= 3:
                patterns.append(pattern)

        return patterns

    def _collect_component_tree(
        self,
        all_components: Dict[str, Any],
        comp_key: str,
        collected: Dict[str, Any],
        depth: int = 0
    ):
        """Collect a component and all its children"""
        if depth > 20 or comp_key in collected:  # Prevent infinite loops
            return

        if comp_key in all_components:
            comp = all_components[comp_key]
            collected[comp_key] = comp

            # Collect children
            children = comp.get('children', {})
            if isinstance(children, dict):
                for child_key in children.keys():
                    self._collect_component_tree(all_components, child_key, collected, depth + 1)

    def _find_subtree_root(self, components: Dict[str, Any]) -> str:
        """Find the root of a component subtree"""
        all_children = set()
        for comp in components.values():
            children = comp.get('children', {})
            if isinstance(children, dict):
                all_children.update(children.keys())

        for comp_key in components:
            if comp_key not in all_children:
                return comp_key

        return list(components.keys())[0] if components else ''


def extract_patterns(input_dir: str, output_dir: str):
    """Main extraction function"""
    extractor = FunctionalPatternExtractor()
    all_patterns: List[FunctionalPattern] = []

    pages_processed = 0

    # Find all page files
    for root, dirs, files in os.walk(input_dir):
        for filename in files:
            if not filename.endswith('.json'):
                continue
            if 'Page' not in root:
                continue

            filepath = os.path.join(root, filename)

            try:
                with open(filepath, 'r') as f:
                    page = json.load(f)

                patterns = extractor.extract_from_page(page, filepath)
                all_patterns.extend(patterns)
                pages_processed += 1

                if pages_processed % 50 == 0:
                    print(f"Processed {pages_processed} pages, found {len(all_patterns)} patterns...")

            except Exception as e:
                print(f"Error processing {filepath}: {e}")

    print(f"\nTotal pages processed: {pages_processed}")
    print(f"Total functional patterns extracted: {len(all_patterns)}")

    # Group by type
    patterns_by_type = defaultdict(list)
    for p in all_patterns:
        patterns_by_type[p.type.value].append(p)

    # Create output directory
    os.makedirs(output_dir, exist_ok=True)

    # Save patterns by type
    for pattern_type, patterns in patterns_by_type.items():
        output_file = os.path.join(output_dir, f"{pattern_type}_patterns.json")

        # Convert to serializable format
        serializable = []
        for p in patterns:
            data = {
                'id': p.id,
                'type': p.type.value,
                'name': p.name,
                'description': p.description,
                'source_page': p.source_page,
                'semantic_tags': p.semantic_tags,
                'component_count': p.component_count,
                'event_count': p.event_count,
                'has_api_calls': p.has_api_calls,
                'has_conditional_logic': p.has_conditional_logic,
                'complexity_score': p.complexity_score,
                'definition': {
                    'rootComponent': p.root_component,
                    'componentDefinition': p.components,
                    'eventFunctions': p.event_functions,
                    'state_paths': list(p.state_paths),
                    'api_endpoints': p.api_endpoints
                }
            }
            serializable.append(data)

        with open(output_file, 'w') as f:
            json.dump(serializable, f, indent=2)

        print(f"  {pattern_type}: {len(patterns)} patterns -> {output_file}")

    # Save summary
    summary = {
        'total_patterns': len(all_patterns),
        'pages_processed': pages_processed,
        'by_type': {t: len(p) for t, p in patterns_by_type.items()},
        'patterns_with_api': sum(1 for p in all_patterns if p.has_api_calls),
        'patterns_with_conditions': sum(1 for p in all_patterns if p.has_conditional_logic),
        'avg_complexity': sum(p.complexity_score for p in all_patterns) / len(all_patterns) if all_patterns else 0
    }

    with open(os.path.join(output_dir, 'summary.json'), 'w') as f:
        json.dump(summary, f, indent=2)

    print(f"\nSummary saved to {output_dir}/summary.json")
    print(f"\nPatterns with API calls: {summary['patterns_with_api']}")
    print(f"Patterns with conditions: {summary['patterns_with_conditions']}")

    return all_patterns


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract functional patterns from pages")
    parser.add_argument(
        "--input-dir",
        default="./definitions",
        help="Directory containing page definitions"
    )
    parser.add_argument(
        "--output-dir",
        default="./extracted_patterns_v3",
        help="Output directory for extracted patterns"
    )

    args = parser.parse_args()
    extract_patterns(args.input_dir, args.output_dir)
