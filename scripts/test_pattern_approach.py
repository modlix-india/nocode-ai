"""
Test Script: Compare Old Multi-Agent vs New Pattern Composition Approach

This script tests both approaches with the same prompts and compares:
1. Output validity (structural correctness)
2. Output completeness (has required components)
3. Event function correctness (valid KIRun format)
4. Generation time
5. Token usage (if available)

Usage:
    python test_pattern_approach.py [--test-case NAME]
"""

import asyncio
import json
import time
import sys
import os
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
from dataclasses import dataclass, asdict
import argparse

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# Test cases with expected outcomes
TEST_CASES = {
    "simple_page": {
        "prompt": "Create a simple page with a welcome text and a Get Started button",
        "expected": {
            "components": ["Grid", "Text", "Button"],
            "min_component_count": 3,
            "should_have_events": True,
        }
    },
    "login_form": {
        "prompt": "Create a login form with email and password fields and a Sign In button",
        "expected": {
            "components": ["Grid", "TextBox", "Button"],
            "min_component_count": 4,
            "should_have_events": True,
            "expected_bindings": ["email", "password"],
        }
    },
    "contact_form": {
        "prompt": "Create a contact form with name, email, message fields and a Submit button",
        "expected": {
            "components": ["Grid", "TextBox", "Button"],
            "min_component_count": 5,
            "should_have_events": True,
        }
    },
    "calculator": {
        "prompt": "Create a simple calculator with number buttons 0-9, plus/minus/multiply/divide operators, and a display",
        "expected": {
            "components": ["Grid", "Button", "Text"],
            "min_component_count": 15,
            "should_have_events": True,
            "min_event_count": 10,  # At least digit buttons
        }
    },
    "navigation": {
        "prompt": "Create a navigation header with logo, Home, About, Contact links",
        "expected": {
            "components": ["Grid", "Image", "Link"],
            "min_component_count": 5,
        }
    },
    "card_list": {
        "prompt": "Create a product listing page with a grid of product cards, each showing image, title, price",
        "expected": {
            "components": ["Grid", "ArrayRepeater", "Text", "Image"],
            "min_component_count": 5,
            "expected_bindings": ["products", "items"],
        }
    },
    "modal_popup": {
        "prompt": "Create a confirmation modal/popup with a message and Yes/No buttons",
        "expected": {
            "components": ["Popup", "Grid", "Text", "Button"],
            "min_component_count": 4,
            "should_have_events": True,
        }
    },
    "dashboard": {
        "prompt": "Create a dashboard page with a sidebar navigation, header, and main content area",
        "expected": {
            "components": ["Grid"],
            "min_component_count": 4,
            "expected_layout": ["header", "sidebar", "main"],
        }
    },
}


@dataclass
class ValidationResult:
    """Result of validating a page definition"""
    is_valid: bool
    has_root_component: bool
    has_flat_structure: bool
    has_valid_children: bool
    has_valid_component_types: bool
    has_valid_events: bool
    component_count: int
    event_count: int
    errors: List[str]
    warnings: List[str]


@dataclass
class TestResult:
    """Result of a single test case"""
    test_name: str
    approach: str  # "old" or "new"
    prompt: str
    success: bool
    validation: ValidationResult
    meets_expectations: bool
    expectation_details: Dict[str, bool]
    generation_time_ms: int
    error: Optional[str] = None
    page_definition: Optional[Dict] = None


class PageValidator:
    """Validates page definitions for structural correctness"""

    VALID_COMPONENT_TYPES = {
        "Grid", "Text", "Button", "TextBox", "Checkbox",
        "RadioButton", "Dropdown", "Image", "Icon", "Link",
        "ArrayRepeater", "Form", "Popup", "Tabs", "Menu",
        "Table", "Video", "Audio", "Calendar", "Chart",
        "Carousel", "Gallery", "ProgressBar", "Stepper",
        "TextArea", "PhoneNumber", "FileUpload", "OTP",
        "TableGrid", "TableColumn", "TableColumns"
    }

    def validate(self, page_def: Dict) -> ValidationResult:
        """Validate a page definition"""
        errors = []
        warnings = []

        # Check root component
        root_component = page_def.get("rootComponent")
        has_root = isinstance(root_component, str) and len(root_component) > 0

        if not has_root:
            if isinstance(root_component, dict):
                errors.append("rootComponent is an object, should be a string key")
            else:
                errors.append("Missing or invalid rootComponent")

        # Check component definition
        comp_def = page_def.get("componentDefinition", {})
        has_flat_structure = True
        has_valid_children = True
        has_valid_types = True
        component_count = len(comp_def)

        # Validate each component
        for comp_key, comp in comp_def.items():
            # Check key matches
            if comp.get("key") != comp_key:
                warnings.append(f"Component key mismatch: {comp_key} vs {comp.get('key')}")

            # Check type
            comp_type = comp.get("type", "")
            if comp_type not in self.VALID_COMPONENT_TYPES:
                errors.append(f"Invalid component type '{comp_type}' in {comp_key}")
                has_valid_types = False

            # Check children structure (should be flat)
            children = comp.get("children", {})
            if isinstance(children, dict):
                for child_key, child_val in children.items():
                    if isinstance(child_val, dict):
                        errors.append(f"Nested child object in {comp_key}.children.{child_key}")
                        has_flat_structure = False
                    elif child_val is not True:
                        warnings.append(f"Child value should be True in {comp_key}.children.{child_key}")

                    # Check child exists
                    if child_key not in comp_def:
                        errors.append(f"Child {child_key} referenced in {comp_key} does not exist")
                        has_valid_children = False

        # Validate event functions
        event_functions = page_def.get("eventFunctions", {})
        event_count = len(event_functions)
        has_valid_events = True

        for event_key, event_def in event_functions.items():
            if not isinstance(event_def, dict):
                errors.append(f"Invalid event function format: {event_key}")
                has_valid_events = False
                continue

            steps = event_def.get("steps", {})
            if not steps:
                warnings.append(f"Event {event_key} has no steps")

            for step_key, step in steps.items():
                if not isinstance(step, dict):
                    errors.append(f"Invalid step format in {event_key}.{step_key}")
                    has_valid_events = False
                    continue

                # Check required fields
                if not step.get("name"):
                    errors.append(f"Missing step name in {event_key}.{step_key}")
                    has_valid_events = False

                # Check parameterMap format
                param_map = step.get("parameterMap", {})
                for param_name, param_values in param_map.items():
                    if isinstance(param_values, dict):
                        for pv_key, pv in param_values.items():
                            if isinstance(pv, dict):
                                if "type" not in pv:
                                    warnings.append(
                                        f"Missing type in {event_key}.{step_key}.{param_name}.{pv_key}"
                                    )

        return ValidationResult(
            is_valid=len(errors) == 0,
            has_root_component=has_root,
            has_flat_structure=has_flat_structure,
            has_valid_children=has_valid_children,
            has_valid_component_types=has_valid_types,
            has_valid_events=has_valid_events,
            component_count=component_count,
            event_count=event_count,
            errors=errors,
            warnings=warnings
        )


class ExpectationChecker:
    """Checks if generated page meets test expectations"""

    def check(
        self,
        page_def: Dict,
        expected: Dict
    ) -> Tuple[bool, Dict[str, bool]]:
        """Check if page meets expectations"""
        details = {}

        comp_def = page_def.get("componentDefinition", {})
        comp_types = [c.get("type", "") for c in comp_def.values()]

        # Check expected component types
        if "components" in expected:
            for exp_type in expected["components"]:
                key = f"has_{exp_type}"
                details[key] = exp_type in comp_types

        # Check minimum component count
        if "min_component_count" in expected:
            details["min_components"] = len(comp_def) >= expected["min_component_count"]

        # Check events
        events = page_def.get("eventFunctions", {})
        if expected.get("should_have_events"):
            details["has_events"] = len(events) > 0

        if "min_event_count" in expected:
            details["min_events"] = len(events) >= expected["min_event_count"]

        # Check expected bindings
        if "expected_bindings" in expected:
            bindings = []
            for comp in comp_def.values():
                bp = comp.get("bindingPath", {})
                if isinstance(bp, dict) and bp.get("value"):
                    bindings.append(bp["value"].lower())

            binding_str = " ".join(bindings)
            for exp_binding in expected["expected_bindings"]:
                key = f"binding_{exp_binding}"
                details[key] = any(exp_binding in b for b in bindings)

        # Check layout structure
        if "expected_layout" in expected:
            comp_names = [c.get("name", "").lower() for c in comp_def.values()]
            for section in expected["expected_layout"]:
                key = f"layout_{section}"
                details[key] = any(section in name for name in comp_names)

        meets_all = all(details.values()) if details else True

        return meets_all, details


class MockLLMClient:
    """Mock LLM client for testing without actual API calls"""

    async def generate(self, prompt: str) -> str:
        """Return mock response based on prompt analysis"""
        await asyncio.sleep(0.1)  # Simulate latency

        # Return appropriate mock response
        if "login" in prompt.lower():
            return json.dumps({
                "page_type": "form",
                "primary_purpose": "User login",
                "search_tags": ["login", "form", "authentication", "email", "password"],
                "components_needed": [
                    {"type": "TextBox", "purpose": "Email input", "label": "Email"},
                    {"type": "TextBox", "purpose": "Password input", "label": "Password"},
                    {"type": "Button", "purpose": "Submit login", "label": "Sign In"}
                ],
                "events_needed": [
                    {"name": "onLogin", "trigger": "onClick", "action": "authenticate user"}
                ],
                "data_bindings": [
                    {"path": "Page.email", "purpose": "email value"},
                    {"path": "Page.password", "purpose": "password value"}
                ]
            })

        # Default response
        return json.dumps({
            "page_type": "other",
            "primary_purpose": "General page",
            "search_tags": prompt.lower().split()[:5],
            "components_needed": [],
            "events_needed": []
        })


async def test_pattern_approach(
    patterns_dir: str,
    test_case_name: Optional[str] = None
):
    """Run tests comparing old and new approaches"""

    # Import pattern composer
    try:
        from app.agents.pattern_composer import PatternComposer, PatternDatabase, SemanticAnalyzer, PatternAdapter
    except ImportError:
        print("Could not import pattern_composer. Make sure you're in the right directory.")
        return

    # Initialize components
    print("Initializing pattern database...")
    db = PatternDatabase(patterns_dir)

    mock_llm = MockLLMClient()
    analyzer = SemanticAnalyzer(mock_llm)
    adapter = PatternAdapter(mock_llm)
    composer = PatternComposer(db, analyzer, adapter)

    validator = PageValidator()
    expectation_checker = ExpectationChecker()

    # Select test cases
    if test_case_name:
        if test_case_name not in TEST_CASES:
            print(f"Unknown test case: {test_case_name}")
            print(f"Available: {list(TEST_CASES.keys())}")
            return
        cases_to_run = {test_case_name: TEST_CASES[test_case_name]}
    else:
        cases_to_run = TEST_CASES

    results: List[TestResult] = []

    print(f"\nRunning {len(cases_to_run)} test cases...\n")
    print("=" * 80)

    for test_name, test_case in cases_to_run.items():
        print(f"\nTest: {test_name}")
        print(f"Prompt: {test_case['prompt'][:60]}...")
        print("-" * 40)

        # Test new pattern-based approach
        try:
            start_time = time.time()
            page_def = await composer.compose_page(test_case["prompt"])
            generation_time = int((time.time() - start_time) * 1000)

            validation = validator.validate(page_def)
            meets_exp, exp_details = expectation_checker.check(
                page_def, test_case["expected"]
            )

            result = TestResult(
                test_name=test_name,
                approach="pattern_composition",
                prompt=test_case["prompt"],
                success=True,
                validation=validation,
                meets_expectations=meets_exp,
                expectation_details=exp_details,
                generation_time_ms=generation_time,
                page_definition=page_def
            )

        except Exception as e:
            result = TestResult(
                test_name=test_name,
                approach="pattern_composition",
                prompt=test_case["prompt"],
                success=False,
                validation=ValidationResult(
                    is_valid=False,
                    has_root_component=False,
                    has_flat_structure=False,
                    has_valid_children=False,
                    has_valid_component_types=False,
                    has_valid_events=False,
                    component_count=0,
                    event_count=0,
                    errors=[str(e)],
                    warnings=[]
                ),
                meets_expectations=False,
                expectation_details={},
                generation_time_ms=0,
                error=str(e)
            )

        results.append(result)

        # Print result summary
        status = "✅" if result.validation.is_valid else "❌"
        exp_status = "✅" if result.meets_expectations else "⚠️"

        print(f"  Pattern Composition: {status} Valid, {exp_status} Expectations")
        print(f"    Components: {result.validation.component_count}")
        print(f"    Events: {result.validation.event_count}")
        print(f"    Time: {result.generation_time_ms}ms")

        if result.validation.errors:
            print(f"    Errors: {result.validation.errors[:3]}")

        if not result.meets_expectations:
            failed = [k for k, v in result.expectation_details.items() if not v]
            print(f"    Failed expectations: {failed}")

    # Print summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    valid_count = sum(1 for r in results if r.validation.is_valid)
    meets_exp_count = sum(1 for r in results if r.meets_expectations)
    avg_time = sum(r.generation_time_ms for r in results) / len(results) if results else 0

    print(f"\nTotal tests: {len(results)}")
    print(f"Valid outputs: {valid_count}/{len(results)} ({100*valid_count/len(results):.1f}%)")
    print(f"Meets expectations: {meets_exp_count}/{len(results)} ({100*meets_exp_count/len(results):.1f}%)")
    print(f"Average generation time: {avg_time:.0f}ms")

    # Save detailed results
    output_file = Path(patterns_dir) / "test_results.json"
    with open(output_file, "w") as f:
        json.dump(
            [asdict(r) for r in results],
            f,
            indent=2,
            default=str
        )
    print(f"\nDetailed results saved to: {output_file}")

    return results


async def compare_with_old_approach(
    old_agent,
    patterns_dir: str,
    test_case_name: str = "login_form"
):
    """
    Compare old multi-agent approach with new pattern composition.

    This requires the old agent to be passed in.
    """
    from app.agents.pattern_composer import PatternComposer, PatternDatabase, SemanticAnalyzer, PatternAdapter

    test_case = TEST_CASES.get(test_case_name)
    if not test_case:
        print(f"Unknown test case: {test_case_name}")
        return

    prompt = test_case["prompt"]
    print(f"\nComparing approaches for: '{prompt}'")
    print("=" * 80)

    validator = PageValidator()

    # Test old approach
    print("\n1. Old Multi-Agent Approach:")
    print("-" * 40)
    try:
        start = time.time()
        old_result = await old_agent.generate(prompt)
        old_time = time.time() - start

        old_page = old_result.get("page", {})
        old_validation = validator.validate(old_page)

        print(f"   Time: {old_time*1000:.0f}ms")
        print(f"   Valid: {old_validation.is_valid}")
        print(f"   Components: {old_validation.component_count}")
        print(f"   Events: {old_validation.event_count}")
        print(f"   Errors: {len(old_validation.errors)}")

    except Exception as e:
        print(f"   Failed: {e}")
        old_validation = None
        old_time = 0

    # Test new approach
    print("\n2. New Pattern Composition Approach:")
    print("-" * 40)

    mock_llm = MockLLMClient()  # Replace with real LLM
    db = PatternDatabase(patterns_dir)
    analyzer = SemanticAnalyzer(mock_llm)
    adapter = PatternAdapter(mock_llm)
    composer = PatternComposer(db, analyzer, adapter)

    try:
        start = time.time()
        new_page = await composer.compose_page(prompt)
        new_time = time.time() - start

        new_validation = validator.validate(new_page)

        print(f"   Time: {new_time*1000:.0f}ms")
        print(f"   Valid: {new_validation.is_valid}")
        print(f"   Components: {new_validation.component_count}")
        print(f"   Events: {new_validation.event_count}")
        print(f"   Errors: {len(new_validation.errors)}")

    except Exception as e:
        print(f"   Failed: {e}")
        new_validation = None
        new_time = 0

    # Comparison
    print("\n3. Comparison:")
    print("-" * 40)

    if old_validation and new_validation:
        print(f"   Validity: Old={old_validation.is_valid}, New={new_validation.is_valid}")
        print(f"   Components: Old={old_validation.component_count}, New={new_validation.component_count}")
        print(f"   Events: Old={old_validation.event_count}, New={new_validation.event_count}")
        print(f"   Time: Old={old_time*1000:.0f}ms, New={new_time*1000:.0f}ms")

        if new_validation.is_valid and not old_validation.is_valid:
            print("\n   ✅ Pattern composition produced valid output where old approach failed!")
        elif new_validation.is_valid and old_validation.is_valid:
            if new_time < old_time:
                print(f"\n   ✅ Pattern composition is {old_time/new_time:.1f}x faster!")


def run_quick_validation_test(patterns_dir: str):
    """Run a quick validation test without LLM calls"""
    print("Running quick validation test...")
    print("=" * 60)

    # Load a sample pattern and validate
    pattern_file = Path(patterns_dir) / "form_pattern_patterns.json"

    if pattern_file.exists():
        with open(pattern_file) as f:
            patterns = json.load(f)

        if patterns:
            sample = patterns[0]
            print(f"\nSample form pattern: {sample.get('name')}")
            print(f"Tags: {sample.get('semantic_tags', [])}")
            print(f"Quality: {sample.get('quality_score', 0)}")

            # Extract and validate
            definition = sample.get("definition", {})
            if "components" in definition:
                validator = PageValidator()

                # Create minimal page from pattern
                page = {
                    "rootComponent": list(definition["components"].keys())[0],
                    "componentDefinition": definition["components"],
                    "eventFunctions": {}
                }

                if definition.get("submitEvent"):
                    page["eventFunctions"]["onSubmit"] = definition["submitEvent"]

                validation = validator.validate(page)

                print(f"\nValidation result:")
                print(f"  Valid: {validation.is_valid}")
                print(f"  Components: {validation.component_count}")
                print(f"  Events: {validation.event_count}")

                if validation.errors:
                    print(f"  Errors: {validation.errors[:3]}")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test pattern composition approach")
    parser.add_argument(
        "--patterns-dir",
        default="./extracted_patterns",
        help="Directory containing extracted patterns"
    )
    parser.add_argument(
        "--test-case",
        help="Specific test case to run"
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Run quick validation test only"
    )

    args = parser.parse_args()

    if args.quick:
        run_quick_validation_test(args.patterns_dir)
    else:
        asyncio.run(test_pattern_approach(args.patterns_dir, args.test_case))
