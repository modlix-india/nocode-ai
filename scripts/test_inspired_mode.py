#!/usr/bin/env python3
"""
Test the inspired-by page generation mode.
This uses LLM agents (Layout, Component, Styles, etc.) to generate pages.
"""
import asyncio
import sys
import json
import logging
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Configure logging to see agent execution
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Set specific loggers to INFO for our agents
for logger_name in ['app.agents.page_agent', 'app.agents.component', 'app.agents.layout',
                    'app.agents.page_generation.executors', 'app.utils.merge']:
    logging.getLogger(logger_name).setLevel(logging.INFO)


async def test_inspired_mode(url: str, instruction: str):
    """Test inspired-by page generation."""
    print(f"\n{'='*60}")
    print(f"Testing Inspired-By Mode")
    print(f"URL: {url}")
    print(f"Instruction: {instruction}")
    print(f"{'='*60}\n")

    from app.agents.page_agent import PageAgent, PageAgentRequest, PageAgentMode, PageAgentOptions

    agent = PageAgent()

    # Use CREATE mode with a URL in the instruction - this triggers inspired-by mode
    request = PageAgentRequest(
        instruction=instruction,
        sourceUrl=url,
        options=PageAgentOptions(mode=PageAgentMode.CREATE)
    )

    print("[Step 1] Executing PageAgent...")
    print("  (This will go through Layout -> Component -> Styles agents)\n")

    try:
        response = await agent.execute(request)

        print(f"\n{'='*60}")
        print("RESULTS")
        print(f"{'='*60}")
        print(f"Success: {response.success}")

        # Analyze the page
        page = response.page
        comp_def = page.get("componentDefinition", {})

        # Count component types
        type_counts = {}
        for key, comp in comp_def.items():
            comp_type = comp.get("type", "Unknown")
            type_counts[comp_type] = type_counts.get(comp_type, 0) + 1

        print(f"\nComponent Breakdown:")
        print(f"  Total components: {len(comp_def)}")
        for comp_type, count in sorted(type_counts.items()):
            print(f"  - {comp_type}: {count}")

        # Check if we have leaf components
        leaf_types = ["Text", "Button", "Image", "Icon", "Link", "TextBox", "Checkbox", "Dropdown", "RadioButton"]
        leaf_count = sum(type_counts.get(t, 0) for t in leaf_types)
        grid_count = type_counts.get("Grid", 0)

        print(f"\n  Leaf components (content): {leaf_count}")
        print(f"  Grid containers: {grid_count}")

        if leaf_count == 0 and grid_count > 0:
            print(f"\n  WARNING: Only Grid containers created, no leaf content!")
            print(f"  This indicates the Component agent is not creating content components.")
        elif leaf_count > 0:
            print(f"\n  SUCCESS: Leaf components created!")

        # Show agent logs
        print(f"\nAgent Logs:")
        for agent_name, log in response.agentLogs.items():
            status = log.status if hasattr(log, 'status') else log.get('status', 'unknown')
            reasoning = log.reasoning if hasattr(log, 'reasoning') else log.get('reasoning', '')
            reasoning_preview = (reasoning[:100] + "...") if reasoning and len(reasoning) > 100 else reasoning
            print(f"  [{agent_name}] {status}: {reasoning_preview}")

        # Save output for inspection
        output_path = Path(__file__).parent / "inspired_page_output.json"
        with open(output_path, "w") as f:
            json.dump(page, f, indent=2)
        print(f"\nSaved page to: {output_path}")

        # Show a sample of components
        print(f"\nSample Components:")
        for i, (key, comp) in enumerate(list(comp_def.items())[:10]):
            comp_type = comp.get("type", "Unknown")
            children_count = len(comp.get("children", {}))
            props = comp.get("properties", {})

            if comp_type == "Text":
                text_val = props.get("text", {}).get("value", "")[:50]
                print(f"  [{i}] {key}: {comp_type} - '{text_val}'")
            elif comp_type == "Button":
                label = props.get("label", {}).get("value", "")
                print(f"  [{i}] {key}: {comp_type} - '{label}'")
            elif comp_type == "Grid":
                children = list(comp.get("children", {}).keys())
                print(f"  [{i}] {key}: {comp_type} - children: {children[:5]}")
            else:
                print(f"  [{i}] {key}: {comp_type}")

        return response.success

    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Test inspired-by page generation")
    parser.add_argument(
        "--url",
        default="https://seera.framer.website/",
        help="URL to use as inspiration"
    )
    parser.add_argument(
        "--instruction",
        default="generate a page that looks like",
        help="Instruction for page generation"
    )

    args = parser.parse_args()

    # Combine instruction with URL if not already included
    instruction = args.instruction
    if args.url not in instruction:
        instruction = f"{instruction} {args.url}"

    success = await test_inspired_mode(args.url, instruction)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
