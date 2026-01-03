"""
Test Page Rendering Script

This script tests generated pages by:
1. PUT-ing them to the actual API
2. Fetching the rendered page
3. Validating the response

Usage:
    python test_page_rendering.py --page-file <path_to_page.json>
    python test_page_rendering.py --test-pattern <pattern_id>

Environment Variables:
    MODLIX_AUTH_TOKEN - Authorization token for API calls
    MODLIX_API_BASE - Base URL (default: https://apps.local.modlix.com)
    MODLIX_TEST_PAGE_ID - Page ID to use for testing
"""

import asyncio
import json
import httpx
import os
import sys
import argparse
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict
from datetime import datetime


@dataclass
class RenderTestResult:
    """Result of a page rendering test"""
    success: bool
    page_id: str
    put_status: int
    put_response: Optional[str]
    render_url: str
    render_time_ms: int
    errors: List[str]
    warnings: List[str]


class PageRenderTester:
    """Tests page rendering via the Modlix API"""

    def __init__(
        self,
        api_base: str = "https://apps.local.modlix.com",
        auth_token: Optional[str] = None
    ):
        self.api_base = api_base.rstrip("/")
        self.auth_token = auth_token or os.environ.get("MODLIX_AUTH_TOKEN")

        if not self.auth_token:
            raise ValueError(
                "Auth token required. Set MODLIX_AUTH_TOKEN env var or pass to constructor."
            )

    def _get_headers(self) -> Dict[str, str]:
        """Get headers for API requests"""
        return {
            "Authorization": self.auth_token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "clientcode": "SYSTEM",
        }

    async def put_page(
        self,
        page_id: str,
        page_definition: Dict[str, Any],
        timeout: float = 30.0,
        app_code: str = "aitestapp",
        client_code: str = "SYSTEM",
        page_name: str = "devTestPage"
    ) -> tuple[int, str]:
        """
        PUT a page definition to the API

        Args:
            page_id: The page ID (MongoDB ObjectId)
            page_definition: The page definition JSON
            timeout: Request timeout in seconds
            app_code: Application code
            client_code: Client code
            page_name: Page name

        Returns:
            Tuple of (status_code, response_text)
        """
        url = f"{self.api_base}/api/ui/pages/{page_id}"

        # Ensure the page definition has required fields
        page_definition["id"] = page_id
        page_definition["name"] = page_name
        page_definition["appCode"] = app_code
        page_definition["clientCode"] = client_code
        page_definition["message"] = "AI Test - Pattern Render"

        async with httpx.AsyncClient(verify=False) as client:
            try:
                response = await client.put(
                    url,
                    headers=self._get_headers(),
                    json=page_definition,
                    timeout=timeout
                )
                return response.status_code, response.text
            except Exception as e:
                return 0, str(e)

    async def get_page(
        self,
        page_id: str,
        timeout: float = 30.0
    ) -> tuple[int, Optional[Dict]]:
        """
        GET a page definition from the API

        Args:
            page_id: The page ID
            timeout: Request timeout in seconds

        Returns:
            Tuple of (status_code, page_definition or None)
        """
        url = f"{self.api_base}/api/ui/pages/{page_id}"

        async with httpx.AsyncClient(verify=False) as client:
            try:
                response = await client.get(
                    url,
                    headers=self._get_headers(),
                    timeout=timeout
                )
                if response.status_code == 200:
                    return response.status_code, response.json()
                return response.status_code, None
            except Exception as e:
                return 0, None

    def get_render_url(
        self,
        app_code: str = "aitestapp",
        client_code: str = "SYSTEM",
        page_name: str = "devTestPage"
    ) -> str:
        """Get the URL where the page can be rendered"""
        return f"{self.api_base}/{app_code}/{client_code}/page/{page_name}"

    async def test_page_render(
        self,
        page_id: str,
        page_definition: Dict[str, Any],
        app_code: str = "aitestapp",
        client_code: str = "SYSTEM",
        page_name: str = "devTestPage"
    ) -> RenderTestResult:
        """
        Test rendering a page by PUT-ing it and checking the response

        Args:
            page_id: Page ID to update
            page_definition: Page definition to test
            app_code: Application code
            client_code: Client code
            page_name: Page name for the URL

        Returns:
            RenderTestResult with test outcome
        """
        errors = []
        warnings = []

        start_time = datetime.now()

        # Step 0: GET the current page to get the version
        get_status, current_page = await self.get_page(page_id)
        if get_status == 200 and current_page:
            # Copy version and other metadata from current page
            page_definition["version"] = current_page.get("version", 1)
            page_definition["createdAt"] = current_page.get("createdAt")
            page_definition["createdBy"] = current_page.get("createdBy")

        # Step 1: PUT the page
        put_status, put_response = await self.put_page(page_id, page_definition, app_code=app_code, client_code=client_code, page_name=page_name)

        render_time_ms = int((datetime.now() - start_time).total_seconds() * 1000)
        render_url = self.get_render_url(app_code, client_code, page_name)

        if put_status != 200:
            errors.append(f"PUT failed with status {put_status}: {put_response}")
            return RenderTestResult(
                success=False,
                page_id=page_id,
                put_status=put_status,
                put_response=put_response,
                render_url=render_url,
                render_time_ms=render_time_ms,
                errors=errors,
                warnings=warnings
            )

        # Parse response to check for errors
        try:
            response_data = json.loads(put_response) if put_response else {}

            # Check for validation errors in response
            if "errors" in response_data:
                for err in response_data["errors"]:
                    errors.append(f"API validation error: {err}")

            if "warnings" in response_data:
                for warn in response_data["warnings"]:
                    warnings.append(f"API warning: {warn}")

        except json.JSONDecodeError:
            pass  # Response might not be JSON

        return RenderTestResult(
            success=len(errors) == 0,
            page_id=page_id,
            put_status=put_status,
            put_response=put_response[:500] if put_response else None,
            render_url=render_url,
            render_time_ms=render_time_ms,
            errors=errors,
            warnings=warnings
        )


class PatternTester:
    """Tests patterns by converting them to full pages and rendering"""

    def __init__(
        self,
        patterns_dir: str,
        render_tester: PageRenderTester
    ):
        self.patterns_dir = Path(patterns_dir)
        self.render_tester = render_tester
        self.patterns: Dict[str, List[Dict]] = {}
        self._load_patterns()

    def _load_patterns(self):
        """Load all patterns from the patterns directory"""
        for pattern_file in self.patterns_dir.glob("*_patterns.json"):
            pattern_type = pattern_file.stem.replace("_patterns", "")
            try:
                with open(pattern_file) as f:
                    self.patterns[pattern_type] = json.load(f)
            except Exception as e:
                print(f"Warning: Could not load {pattern_file}: {e}")

    def get_pattern_by_id(self, pattern_id: str) -> Optional[Dict]:
        """Find a pattern by its ID"""
        for pattern_type, patterns in self.patterns.items():
            for pattern in patterns:
                if pattern.get("id") == pattern_id:
                    return pattern
        return None

    def list_patterns(
        self,
        pattern_type: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 20
    ) -> List[Dict]:
        """List available patterns"""
        results = []

        for ptype, patterns in self.patterns.items():
            if pattern_type and ptype != pattern_type:
                continue

            for pattern in patterns:
                if category and pattern.get("category") != category:
                    continue

                results.append({
                    "id": pattern.get("id"),
                    "name": pattern.get("name"),
                    "type": ptype,
                    "category": pattern.get("category"),
                    "tags": pattern.get("semantic_tags", [])[:5],
                    "quality": pattern.get("quality_score", 0)
                })

                if len(results) >= limit:
                    return results

        return results

    def pattern_to_page(self, pattern: Dict) -> Dict[str, Any]:
        """Convert a pattern to a testable page definition"""
        definition = pattern.get("definition", {})
        pattern_type = pattern.get("type", "")

        # Handle V3 functional patterns (login_form, crud_form, data_table, etc.)
        if pattern_type in ("login_form", "crud_form", "data_table", "data_list",
                           "modal_confirm", "navigation", "calculator", "generic",
                           "contact_form", "signup_form", "file_upload", "step_wizard"):
            # V3 patterns have complete structure
            return {
                "rootComponent": definition.get("rootComponent"),
                "componentDefinition": definition.get("componentDefinition", {}),
                "eventFunctions": definition.get("eventFunctions", {}),
                "properties": {}
            }

        if pattern_type == "component_tree":
            # Component tree patterns have components
            components = definition.get("components", {})
            if not components:
                components = definition

            # Use rootKey if available, otherwise find root
            root = definition.get("rootKey")

            if not root:
                # Find root (component with no parent or first one)
                all_children = set()
                for comp_key, comp in components.items():
                    children = comp.get("children", {})
                    if isinstance(children, dict):
                        all_children.update(children.keys())

                for comp_key in components:
                    if comp_key not in all_children:
                        root = comp_key
                        break

            if not root:
                root = list(components.keys())[0] if components else "root"

            return {
                "rootComponent": root,
                "componentDefinition": components,
                "eventFunctions": definition.get("events", {}),
                "properties": {}
            }

        elif pattern_type == "event_function":
            # Event patterns need a minimal UI to test
            return {
                "rootComponent": "testGrid",
                "componentDefinition": {
                    "testGrid": {
                        "key": "testGrid",
                        "type": "Grid",
                        "name": "Test Grid",
                        "children": {
                            "testButton": True
                        }
                    },
                    "testButton": {
                        "key": "testButton",
                        "type": "Button",
                        "name": "Test Button",
                        "properties": {
                            "label": {"value": "Test Event"}
                        }
                    }
                },
                "eventFunctions": {
                    "testEvent": definition
                },
                "properties": {}
            }

        elif pattern_type == "form_pattern":
            # Form patterns
            return {
                "rootComponent": definition.get("rootComponent", "formRoot"),
                "componentDefinition": definition.get("components", {}),
                "eventFunctions": {
                    "onSubmit": definition.get("submitEvent", {})
                } if definition.get("submitEvent") else {},
                "properties": {}
            }

        else:
            # Generic pattern - try to use as-is
            return {
                "rootComponent": definition.get("rootComponent", list(definition.keys())[0] if definition else "root"),
                "componentDefinition": definition.get("componentDefinition", definition.get("components", {})),
                "eventFunctions": definition.get("eventFunctions", definition.get("events", {})),
                "properties": definition.get("properties", {})
            }

    async def test_pattern(
        self,
        pattern_id: str,
        page_id: str
    ) -> RenderTestResult:
        """Test a specific pattern by ID"""
        pattern = self.get_pattern_by_id(pattern_id)
        if not pattern:
            return RenderTestResult(
                success=False,
                page_id=page_id,
                put_status=0,
                put_response=None,
                render_url="",
                render_time_ms=0,
                errors=[f"Pattern not found: {pattern_id}"],
                warnings=[]
            )

        page = self.pattern_to_page(pattern)
        return await self.render_tester.test_page_render(page_id, page)

    async def batch_test(
        self,
        n_per_type: int,
        page_id: str
    ) -> List[Dict[str, Any]]:
        """Test N random patterns from each pattern type"""
        import random

        results = []

        for pattern_type, patterns in self.patterns.items():
            print(f"\nTesting {pattern_type} patterns...")

            # Get patterns with component counts between 3-50 for reasonable tests
            testable = []
            for p in patterns:
                defn = p.get("definition", {})
                # V3 uses componentDefinition, V2 uses components
                comp_count = len(defn.get("componentDefinition", defn.get("components", {})))
                if 3 <= comp_count <= 50:
                    testable.append(p)

            # Sample N patterns
            sample_size = min(n_per_type, len(testable))
            if sample_size == 0:
                print(f"  No testable patterns found for {pattern_type}")
                continue

            sample = random.sample(testable, sample_size)

            for i, pattern in enumerate(sample, 1):
                pattern_id = pattern.get("id", "unknown")
                pattern_name = pattern.get("name", "unnamed")[:30]
                defn = pattern.get("definition", {})
                comp_count = len(defn.get("componentDefinition", defn.get("components", {})))

                print(f"  [{i}/{sample_size}] {pattern_name} ({comp_count} components)...", end=" ", flush=True)

                try:
                    page = self.pattern_to_page(pattern)
                    result = await self.render_tester.test_page_render(page_id, page)

                    status = "✅" if result.success else "❌"
                    print(f"{status} ({result.render_time_ms}ms)")

                    results.append({
                        "pattern_id": pattern_id,
                        "pattern_name": pattern_name,
                        "pattern_type": pattern_type,
                        "component_count": comp_count,
                        "success": result.success,
                        "status_code": result.put_status,
                        "time_ms": result.render_time_ms,
                        "errors": result.errors
                    })

                except Exception as e:
                    print(f"❌ Error: {e}")
                    results.append({
                        "pattern_id": pattern_id,
                        "pattern_name": pattern_name,
                        "pattern_type": pattern_type,
                        "component_count": comp_count,
                        "success": False,
                        "status_code": 0,
                        "time_ms": 0,
                        "errors": [str(e)]
                    })

                # Small delay between tests
                await asyncio.sleep(0.5)

        return results

    def generate_complex_page(self) -> Dict[str, Any]:
        """Generate a complex page by composing multiple patterns together"""
        import random

        # Find good patterns for each section
        def find_patterns_by_tags(tags: List[str], pattern_type: str = "component_tree", max_components: int = 30) -> List[Dict]:
            matches = []
            for p in self.patterns.get(pattern_type, []):
                p_tags = p.get("semantic_tags", [])
                comp_count = len(p.get("definition", {}).get("components", {}))
                if comp_count <= max_components and any(t in p_tags for t in tags):
                    matches.append(p)
            return matches

        # Find a header/nav pattern
        nav_patterns = find_patterns_by_tags(["navigation", "header", "nav", "menu"], max_components=15)
        # Find a form pattern
        form_patterns = find_patterns_by_tags(["form", "form-with-submit", "input"], max_components=20)
        # Find a content/card pattern
        content_patterns = find_patterns_by_tags(["card", "content-block", "gallery", "listing"], max_components=25)
        # Find a footer pattern
        footer_patterns = find_patterns_by_tags(["footer"], max_components=10)

        # Build the complex page
        components = {}
        children = {}

        # Create root grid
        root_key = "complexPageRoot"
        components[root_key] = {
            "key": root_key,
            "name": "Complex Page Root",
            "type": "Grid",
            "properties": {},
            "styleProperties": {
                "mainStyle": {
                    "resolutions": {
                        "ALL": {
                            "width": {"value": "100vw"},
                            "minHeight": {"value": "100vh"},
                            "gap": {"value": "0px"},
                            "flexDirection": {"value": "column"}
                        }
                    }
                }
            },
            "children": {}
        }

        section_index = 0

        # Add nav section if found
        if nav_patterns:
            nav = random.choice(nav_patterns[:5])  # Pick from top 5
            nav_components = nav.get("definition", {}).get("components", {})
            nav_root = nav.get("definition", {}).get("rootKey")

            if nav_root and nav_root in nav_components:
                # Prefix all keys to avoid conflicts
                prefix = f"nav_{section_index}_"
                for old_key, comp in nav_components.items():
                    new_key = prefix + old_key
                    new_comp = json.loads(json.dumps(comp))  # Deep copy
                    new_comp["key"] = new_key

                    # Update children references
                    if "children" in new_comp:
                        new_children = {}
                        for child_key in new_comp["children"]:
                            new_children[prefix + child_key] = True
                        new_comp["children"] = new_children

                    components[new_key] = new_comp

                children[prefix + nav_root] = True
                section_index += 1

        # Add content section if found
        if content_patterns:
            content = random.choice(content_patterns[:5])
            content_components = content.get("definition", {}).get("components", {})
            content_root = content.get("definition", {}).get("rootKey")

            if content_root and content_root in content_components:
                prefix = f"content_{section_index}_"
                for old_key, comp in content_components.items():
                    new_key = prefix + old_key
                    new_comp = json.loads(json.dumps(comp))
                    new_comp["key"] = new_key

                    if "children" in new_comp:
                        new_children = {}
                        for child_key in new_comp["children"]:
                            new_children[prefix + child_key] = True
                        new_comp["children"] = new_children

                    components[new_key] = new_comp

                children[prefix + content_root] = True
                section_index += 1

        # Add form section if found
        if form_patterns:
            form = random.choice(form_patterns[:5])
            form_components = form.get("definition", {}).get("components", {})
            form_root = form.get("definition", {}).get("rootKey")

            if form_root and form_root in form_components:
                prefix = f"form_{section_index}_"
                for old_key, comp in form_components.items():
                    new_key = prefix + old_key
                    new_comp = json.loads(json.dumps(comp))
                    new_comp["key"] = new_key

                    if "children" in new_comp:
                        new_children = {}
                        for child_key in new_comp["children"]:
                            new_children[prefix + child_key] = True
                        new_comp["children"] = new_children

                    components[new_key] = new_comp

                children[prefix + form_root] = True
                section_index += 1

        # Add footer section if found
        if footer_patterns:
            footer = random.choice(footer_patterns[:3])
            footer_components = footer.get("definition", {}).get("components", {})
            footer_root = footer.get("definition", {}).get("rootKey")

            if footer_root and footer_root in footer_components:
                prefix = f"footer_{section_index}_"
                for old_key, comp in footer_components.items():
                    new_key = prefix + old_key
                    new_comp = json.loads(json.dumps(comp))
                    new_comp["key"] = new_key

                    if "children" in new_comp:
                        new_children = {}
                        for child_key in new_comp["children"]:
                            new_children[prefix + child_key] = True
                        new_comp["children"] = new_children

                    components[new_key] = new_comp

                children[prefix + footer_root] = True

        # Update root's children
        components[root_key]["children"] = children

        return {
            "rootComponent": root_key,
            "componentDefinition": components,
            "eventFunctions": {},
            "properties": {}
        }


async def main():
    parser = argparse.ArgumentParser(description="Test page rendering via Modlix API")

    parser.add_argument(
        "--page-file",
        help="Path to a page definition JSON file to test"
    )
    parser.add_argument(
        "--test-pattern",
        help="ID of a pattern to test"
    )
    parser.add_argument(
        "--batch-test",
        type=int,
        metavar="N",
        help="Test N random patterns from each type"
    )
    parser.add_argument(
        "--generate-complex",
        action="store_true",
        help="Generate and test a complex page by composing multiple patterns"
    )
    parser.add_argument(
        "--list-patterns",
        action="store_true",
        help="List available patterns"
    )
    parser.add_argument(
        "--pattern-type",
        help="Filter patterns by type"
    )
    parser.add_argument(
        "--category",
        help="Filter patterns by category"
    )
    parser.add_argument(
        "--patterns-dir",
        default="./extracted_patterns_v2",
        help="Directory containing extracted patterns"
    )
    parser.add_argument(
        "--page-id",
        default=os.environ.get("MODLIX_TEST_PAGE_ID", "6958c4882b10ab60598fa975"),
        help="Page ID to use for testing"
    )
    parser.add_argument(
        "--api-base",
        default=os.environ.get("MODLIX_API_BASE", "https://apps.dev.modlix.com"),
        help="API base URL"
    )
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("MODLIX_AUTH_TOKEN"),
        help="Authorization token"
    )
    parser.add_argument(
        "--output",
        help="Output file for results (JSON)"
    )

    args = parser.parse_args()

    # Initialize tester
    if not args.auth_token and not args.list_patterns:
        print("Error: Auth token required. Set MODLIX_AUTH_TOKEN or use --auth-token")
        print("\nYou can get the token from the browser's Developer Tools:")
        print("1. Open https://apps.local.modlix.com")
        print("2. Open Developer Tools (F12)")
        print("3. Go to Network tab")
        print("4. Make any API request")
        print("5. Copy the Authorization header value")
        sys.exit(1)

    # List patterns mode
    if args.list_patterns:
        pattern_tester = PatternTester(args.patterns_dir, None)
        patterns = pattern_tester.list_patterns(
            pattern_type=args.pattern_type,
            category=args.category
        )

        print(f"\nFound {len(patterns)} patterns:\n")
        print(f"{'ID':<40} {'Name':<30} {'Type':<20} {'Category':<15} {'Quality':<8}")
        print("-" * 120)

        for p in patterns:
            pid = (p.get('id') or 'N/A')[:38]
            pname = (p.get('name') or 'N/A')[:28]
            ptype = (p.get('type') or 'N/A')[:18]
            pcat = (p.get('category') or 'N/A')[:13]
            pqual = p.get('quality') or 0
            print(f"{pid:<40} {pname:<30} {ptype:<20} {pcat:<15} {pqual:.2f}")

        print(f"\nTotal patterns by type:")
        for ptype, plist in pattern_tester.patterns.items():
            print(f"  {ptype}: {len(plist)}")

        return

    # Create render tester
    render_tester = PageRenderTester(
        api_base=args.api_base,
        auth_token=args.auth_token
    )

    results = []

    # Test page file
    if args.page_file:
        print(f"\nTesting page file: {args.page_file}")
        print("=" * 60)

        with open(args.page_file) as f:
            page_def = json.load(f)

        result = await render_tester.test_page_render(args.page_id, page_def)
        results.append(result)

        print(f"\nResult:")
        print(f"  Success: {'✅' if result.success else '❌'}")
        print(f"  PUT Status: {result.put_status}")
        print(f"  Render URL: {result.render_url}")
        print(f"  Time: {result.render_time_ms}ms")

        if result.errors:
            print(f"  Errors:")
            for err in result.errors:
                print(f"    - {err}")

        if result.warnings:
            print(f"  Warnings:")
            for warn in result.warnings:
                print(f"    - {warn}")

    # Test pattern
    if args.test_pattern:
        print(f"\nTesting pattern: {args.test_pattern}")
        print("=" * 60)

        pattern_tester = PatternTester(args.patterns_dir, render_tester)
        result = await pattern_tester.test_pattern(args.test_pattern, args.page_id)
        results.append(result)

        print(f"\nResult:")
        print(f"  Success: {'✅' if result.success else '❌'}")
        print(f"  PUT Status: {result.put_status}")
        print(f"  Render URL: {result.render_url}")
        print(f"  Time: {result.render_time_ms}ms")

        if result.errors:
            print(f"  Errors:")
            for err in result.errors:
                print(f"    - {err}")

    # Batch test
    if args.batch_test:
        print(f"\nBatch Testing: {args.batch_test} patterns per type")
        print("=" * 60)

        pattern_tester = PatternTester(args.patterns_dir, render_tester)
        batch_results = await pattern_tester.batch_test(args.batch_test, args.page_id)

        # Summary
        print("\n" + "=" * 60)
        print("BATCH TEST SUMMARY")
        print("=" * 60)

        total = len(batch_results)
        success = sum(1 for r in batch_results if r["success"])
        failed = total - success

        print(f"\nTotal tested: {total}")
        print(f"Success: {success} ({100*success/total:.1f}%)" if total > 0 else "Success: 0")
        print(f"Failed: {failed}")

        if failed > 0:
            print(f"\nFailed patterns:")
            for r in batch_results:
                if not r["success"]:
                    print(f"  - {r['pattern_name']} ({r['pattern_type']}): {r['errors'][0][:80] if r['errors'] else 'Unknown error'}")

        # By type summary
        print(f"\nBy pattern type:")
        type_stats = {}
        for r in batch_results:
            ptype = r["pattern_type"]
            if ptype not in type_stats:
                type_stats[ptype] = {"total": 0, "success": 0}
            type_stats[ptype]["total"] += 1
            if r["success"]:
                type_stats[ptype]["success"] += 1

        for ptype, stats in type_stats.items():
            pct = 100 * stats["success"] / stats["total"] if stats["total"] > 0 else 0
            print(f"  {ptype}: {stats['success']}/{stats['total']} ({pct:.0f}%)")

        # Save batch results
        if args.output:
            output_path = Path(args.output)
            with open(output_path, "w") as f:
                json.dump(batch_results, f, indent=2)
            print(f"\nBatch results saved to: {output_path}")

    # Generate complex page
    if args.generate_complex:
        print(f"\nGenerating Complex Page")
        print("=" * 60)

        pattern_tester = PatternTester(args.patterns_dir, render_tester)
        complex_page = pattern_tester.generate_complex_page()

        comp_count = len(complex_page.get("componentDefinition", {}))
        print(f"Generated page with {comp_count} components")

        # Save the generated page for inspection
        generated_path = Path(args.patterns_dir) / "generated_complex_page.json"
        with open(generated_path, "w") as f:
            json.dump(complex_page, f, indent=2)
        print(f"Saved to: {generated_path}")

        # Test it
        print(f"\nTesting generated page...")
        result = await render_tester.test_page_render(args.page_id, complex_page)

        print(f"\nResult:")
        print(f"  Success: {'✅' if result.success else '❌'}")
        print(f"  PUT Status: {result.put_status}")
        print(f"  Render URL: {result.render_url}")
        print(f"  Time: {result.render_time_ms}ms")
        print(f"  Components: {comp_count}")

        if result.errors:
            print(f"  Errors:")
            for err in result.errors:
                print(f"    - {err}")

    # Save results
    if args.output and results and not args.batch_test:
        output_path = Path(args.output)
        with open(output_path, "w") as f:
            json.dump([asdict(r) for r in results], f, indent=2)
        print(f"\nResults saved to: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
