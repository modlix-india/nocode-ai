#!/usr/bin/env python3
"""
Test website import end-to-end.
Clones a website and pushes it to the Modlix dev server.
"""
import asyncio
import sys
import json
import aiohttp
from pathlib import Path
from datetime import datetime

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))


# Dev server config
BASE_URL = "https://apps.dev.modlix.com"
AUTH_TOKEN = "eyJhbGciOiJIUzUxMiJ9.eyJob3N0TmFtZSI6ImFwcHMuZGV2Lm1vZGxpeC5jb20iLCJwb3J0IjoiODAiLCJsb2dnZWRJbkNsaWVudElkIjoxLCJsb2dnZWRJbkNsaWVudENvZGUiOiJTWVNURU0iLCJhcHBDb2RlIjoiYXBwYnVpbGRlciIsIm9uZVRpbWUiOmZhbHNlLCJ1c2VySWQiOjE0MiwiaWF0IjoxNzYzNTI3MDM3LCJleHAiOjE3OTUwNjMwMzd9.r8GR782tLyYX8NRrZP8cA9J-7wxGFtec_DIx9oPfcvbrudoIZx4R3C2fnvNl58jiT9HWMAGULuFySufg3daSmQ"
PAGE_ID = "6958c4882b10ab60598fa975"
CLIENT_CODE = "SYSTEM"


async def test_website_import(url: str):
    """Test importing a website and pushing to dev server."""
    print(f"\n{'='*60}")
    print(f"Testing Website Import: {url}")
    print(f"{'='*60}")

    # Step 1: Extract website using website_extractor
    print("\n[Step 1] Extracting website with Playwright...")
    from app.services.website_extractor import get_website_extractor

    extractor = get_website_extractor()
    try:
        visual_data = await extractor.extract(url)
        print(f"  Elements extracted: {len(visual_data.elements)}")
        print(f"  Images found: {len(visual_data.images)}")
        print(f"  Root styles (by viewport):")
        for viewport, styles in visual_data.root_styles.items():
            print(f"    {viewport}: {len(styles)} properties")
            # Show all root style properties
            for prop, value in sorted(styles.items()):
                val_preview = str(value)[:50] + "..." if len(str(value)) > 50 else value
                print(f"      - {prop}: {val_preview}")

        # Check viewport styles on first few elements
        print(f"\n  Sample element viewport styles:")
        for i, elem in enumerate(visual_data.elements[:3]):
            desktop_count = len(elem.styles.get("desktop", {}))
            tablet_count = len(elem.styles.get("tablet", {}))
            mobile_count = len(elem.styles.get("mobile", {}))
            print(f"    [{i}] {elem.tag} #{elem.id[:20] if elem.id else 'no-id'}: desktop={desktop_count}, tablet={tablet_count}, mobile={mobile_count}")
    except Exception as e:
        print(f"  ERROR extracting website: {e}")
        await extractor.close()
        return False
    finally:
        await extractor.close()

    # Step 2: Convert to Nocode page using PageAgent
    print("\n[Step 2] Converting to Nocode page definition...")
    from app.agents.page_agent import PageAgent, PageAgentRequest, PageAgentMode, PageAgentOptions

    agent = PageAgent()
    request = PageAgentRequest(
        instruction=f"Clone {url} exactly",
        sourceUrl=url,
        clientCode=CLIENT_CODE,
        options=PageAgentOptions(mode=PageAgentMode.IMPORT)
    )

    try:
        # Use _convert_visual_to_nocode directly for testing
        page_def = agent._convert_visual_to_nocode(visual_data, {})

        component_count = len(page_def.get("componentDefinition", {}))
        print(f"  Components created: {component_count}")

        # Check responsive styles in output
        print(f"\n  Sample component responsive styles:")
        comp_def = page_def.get("componentDefinition", {})
        sample_keys = list(comp_def.keys())[:5]

        responsive_count = {"ALL": 0, "TABLET": 0, "MOBILE": 0}
        for key in sample_keys:
            comp = comp_def[key]
            style_props = comp.get("styleProperties", {})
            # styleProperties now uses unique IDs as keys, not "rootStyle"
            # Get the first style entry to check resolutions
            resolutions = {}
            for style_key, style_val in style_props.items():
                resolutions = style_val.get("resolutions", {})
                break  # Check first entry

            has_all = "ALL" in resolutions
            has_tablet = "TABLET_LANDSCAPE_SCREEN_SMALL" in resolutions
            has_mobile = "MOBILE_LANDSCAPE_SCREEN_SMALL" in resolutions

            if has_all:
                responsive_count["ALL"] += 1
            if has_tablet:
                responsive_count["TABLET"] += 1
            if has_mobile:
                responsive_count["MOBILE"] += 1

            print(f"    {key[:30]}: ALL={has_all}, TABLET={has_tablet}, MOBILE={has_mobile}")

        # Count total responsive styles
        total_with_tablet = 0
        total_with_mobile = 0
        for comp in comp_def.values():
            style_props = comp.get("styleProperties", {})
            for style_key, style_val in style_props.items():
                resolutions = style_val.get("resolutions", {})
                if "TABLET_LANDSCAPE_SCREEN_SMALL" in resolutions:
                    total_with_tablet += 1
                if "MOBILE_LANDSCAPE_SCREEN_SMALL" in resolutions:
                    total_with_mobile += 1
                break  # Check first entry only

        print(f"\n  Total components with tablet styles: {total_with_tablet}/{component_count}")
        print(f"  Total components with mobile styles: {total_with_mobile}/{component_count}")

    except Exception as e:
        print(f"  ERROR converting page: {e}")
        import traceback
        traceback.print_exc()
        return False

    # Step 3: Push to dev server
    print("\n[Step 3] Pushing to dev server...")

    headers = {
        "Authorization": AUTH_TOKEN,
        "clientcode": CLIENT_CODE,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    async with aiohttp.ClientSession() as session:
        # First GET the page to get current version
        get_url = f"{BASE_URL}/api/ui/pages/{PAGE_ID}"
        async with session.get(get_url, headers=headers) as resp:
            if resp.status == 200:
                current_page = await resp.json()
                current_version = current_page.get("version", 1)
                print(f"  Current page version: {current_version}")
            else:
                print(f"  Could not get current page: {resp.status}")
                current_version = 1

        # Update page_def with required fields
        page_def["id"] = PAGE_ID
        page_def["name"] = "devTestPage"
        page_def["appCode"] = "appbuilder"
        page_def["clientCode"] = CLIENT_CODE
        page_def["version"] = current_version
        page_def["message"] = f"Website import test: {url}"

        if "properties" not in page_def:
            page_def["properties"] = {}
        if "eventFunctions" not in page_def:
            page_def["eventFunctions"] = {}

        # PUT the page
        put_url = f"{BASE_URL}/api/ui/pages/{PAGE_ID}"

        async with session.put(put_url, headers=headers, json=page_def) as resp:
            status = resp.status
            response_text = await resp.text()

            print(f"  PUT Status: {status}")

            if status == 200:
                print(f"  SUCCESS! Page updated.")
                render_url = f"{BASE_URL}/api/ui/page/appbuilder/devTestPage"
                print(f"  Render URL: {render_url}")
            else:
                print(f"  FAILED: {response_text[:500]}")
                return False

    # Step 4: Save page definition for inspection
    print("\n[Step 4] Saving page definition for inspection...")
    output_path = Path(__file__).parent / "imported_page.json"
    with open(output_path, "w") as f:
        json.dump(page_def, f, indent=2)
    print(f"  Saved to: {output_path}")

    # Summary
    print(f"\n{'='*60}")
    print("IMPORT TEST SUMMARY")
    print(f"{'='*60}")
    print(f"Source URL: {url}")
    print(f"Elements extracted: {len(visual_data.elements)}")
    print(f"Components created: {component_count}")
    print(f"Components with tablet styles: {total_with_tablet}")
    print(f"Components with mobile styles: {total_with_mobile}")
    print(f"Page URL: {BASE_URL}/api/ui/page/appbuilder/devTestPage")
    print(f"Status: SUCCESS")

    return True


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Test website import")
    parser.add_argument(
        "--url",
        default="https://ceo.pronexus.in/",
        help="URL to import"
    )

    args = parser.parse_args()

    success = await test_website_import(args.url)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
