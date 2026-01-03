#!/usr/bin/env python3
"""
Test script to verify responsive styles are correctly extracted from website imports.
Tests that _build_element_styles properly extracts desktop, tablet, and mobile CSS.
"""
import asyncio
import sys
import json
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dataclasses import dataclass, field
from typing import Dict, List, Any


@dataclass
class MockVisualElement:
    """Mock VisualElement for testing"""
    id: str
    tag: str
    text: str = ""
    image_url: str = ""
    styles: Dict[str, Dict[str, str]] = field(default_factory=dict)
    bounds: Dict[str, Any] = field(default_factory=dict)
    attributes: Dict[str, str] = field(default_factory=dict)
    children: List["MockVisualElement"] = field(default_factory=list)


def create_test_element() -> MockVisualElement:
    """Create a test element with different styles at each viewport."""
    return MockVisualElement(
        id="test_div",
        tag="div",
        text="",
        styles={
            "desktop": {
                "display": "flex",
                "flexDirection": "row",
                "gap": "20px",
                "padding": "40px",
                "fontSize": "18px",
                "width": "1200px",
            },
            "tablet": {
                "display": "flex",
                "flexDirection": "column",  # Different from desktop
                "gap": "15px",  # Different from desktop
                "padding": "30px",  # Different from desktop
                "fontSize": "16px",  # Different from desktop
                "width": "100%",  # Different from desktop
            },
            "mobile": {
                "display": "block",  # Different from desktop
                "flexDirection": "column",  # Different from desktop
                "gap": "10px",  # Different from desktop
                "padding": "20px",  # Different from desktop
                "fontSize": "14px",  # Different from desktop
                "width": "100%",  # Different from desktop
            },
        },
        children=[]
    )


def create_text_element() -> MockVisualElement:
    """Create a test Text element with responsive typography."""
    return MockVisualElement(
        id="test_heading",
        tag="h1",
        text="Hello World",
        styles={
            "desktop": {
                "fontSize": "48px",
                "lineHeight": "1.2",
                "color": "#000000",
                "textAlign": "left",
            },
            "tablet": {
                "fontSize": "36px",  # Smaller on tablet
                "lineHeight": "1.3",
                "color": "#000000",
                "textAlign": "center",  # Center on tablet
            },
            "mobile": {
                "fontSize": "24px",  # Smaller on mobile
                "lineHeight": "1.4",
                "color": "#333333",  # Slightly different color
                "textAlign": "center",
            },
        },
        children=[]
    )


def create_image_element() -> MockVisualElement:
    """Create a test Image element with responsive sizing."""
    return MockVisualElement(
        id="test_image",
        tag="img",
        image_url="https://example.com/image.jpg",
        styles={
            "desktop": {
                "width": "500px",
                "height": "300px",
                "objectFit": "cover",
            },
            "tablet": {
                "width": "100%",  # Full width on tablet
                "height": "250px",
                "objectFit": "cover",
            },
            "mobile": {
                "width": "100%",
                "height": "200px",  # Shorter on mobile
                "objectFit": "contain",  # Different fit
            },
        },
        children=[]
    )


async def test_responsive_styles():
    """Test that _build_element_styles correctly handles all viewports."""
    from app.agents.page_agent import PageAgent

    agent = PageAgent()

    print("=" * 60)
    print("Testing Responsive Style Extraction")
    print("=" * 60)

    # Test 1: Grid component
    print("\n--- Test 1: Grid Component ---")
    grid_elem = create_test_element()
    grid_styles = agent._build_element_styles(grid_elem, "Grid")

    root_style = grid_styles.get("rootStyle", {})
    resolutions = root_style.get("resolutions", {})

    print(f"Resolutions found: {list(resolutions.keys())}")

    # Check ALL (desktop)
    all_styles = resolutions.get("ALL", {})
    print(f"\nALL (desktop) styles count: {len(all_styles)}")
    print(f"  display: {all_styles.get('display', {}).get('value', 'NOT SET')}")
    print(f"  flexDirection: {all_styles.get('flexDirection', {}).get('value', 'NOT SET')}")
    print(f"  gap: {all_styles.get('gap', {}).get('value', 'NOT SET')}")

    # Check TABLET_POTRAIT_SCREEN
    tablet_styles = resolutions.get("TABLET_POTRAIT_SCREEN", {})
    print(f"\nTABLET_POTRAIT_SCREEN styles count: {len(tablet_styles)}")
    if tablet_styles:
        for prop, val in tablet_styles.items():
            print(f"  {prop}: {val.get('value', 'NOT SET')}")
    else:
        print("  NO TABLET STYLES - BUG!")

    # Check MOBILE_POTRAIT_SCREEN
    mobile_styles = resolutions.get("MOBILE_POTRAIT_SCREEN", {})
    print(f"\nMOBILE_POTRAIT_SCREEN styles count: {len(mobile_styles)}")
    if mobile_styles:
        for prop, val in mobile_styles.items():
            print(f"  {prop}: {val.get('value', 'NOT SET')}")
    else:
        print("  NO MOBILE STYLES - BUG!")

    # Test 2: Text component
    print("\n--- Test 2: Text Component ---")
    text_elem = create_text_element()
    text_styles = agent._build_element_styles(text_elem, "Text")

    text_style = text_styles.get("textStyle", {})
    text_resolutions = text_style.get("resolutions", {})

    print(f"textStyle resolutions found: {list(text_resolutions.keys())}")

    if "TABLET_POTRAIT_SCREEN" in text_resolutions:
        print(f"Tablet typography: {text_resolutions['TABLET_POTRAIT_SCREEN']}")
    else:
        print("NO TABLET TYPOGRAPHY - BUG!")

    if "MOBILE_POTRAIT_SCREEN" in text_resolutions:
        print(f"Mobile typography: {text_resolutions['MOBILE_POTRAIT_SCREEN']}")
    else:
        print("NO MOBILE TYPOGRAPHY - BUG!")

    # Test 3: Image component
    print("\n--- Test 3: Image Component ---")
    image_elem = create_image_element()
    image_styles = agent._build_element_styles(image_elem, "Image")

    image_style = image_styles.get("imageStyle", {})
    image_resolutions = image_style.get("resolutions", {})

    print(f"imageStyle resolutions found: {list(image_resolutions.keys())}")

    if "TABLET_POTRAIT_SCREEN" in image_resolutions:
        print(f"Tablet image: {image_resolutions['TABLET_POTRAIT_SCREEN']}")
    else:
        print("NO TABLET IMAGE STYLES - BUG!")

    if "MOBILE_POTRAIT_SCREEN" in image_resolutions:
        print(f"Mobile image: {image_resolutions['MOBILE_POTRAIT_SCREEN']}")
    else:
        print("NO MOBILE IMAGE STYLES - BUG!")

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    grid_has_tablet = bool(resolutions.get("TABLET_POTRAIT_SCREEN"))
    grid_has_mobile = bool(resolutions.get("MOBILE_POTRAIT_SCREEN"))
    text_has_tablet = bool(text_resolutions.get("TABLET_POTRAIT_SCREEN"))
    text_has_mobile = bool(text_resolutions.get("MOBILE_POTRAIT_SCREEN"))
    image_has_tablet = bool(image_resolutions.get("TABLET_POTRAIT_SCREEN"))
    image_has_mobile = bool(image_resolutions.get("MOBILE_POTRAIT_SCREEN"))

    all_pass = all([
        grid_has_tablet, grid_has_mobile,
        text_has_tablet, text_has_mobile,
        image_has_tablet, image_has_mobile
    ])

    print(f"Grid tablet styles:  {'PASS' if grid_has_tablet else 'FAIL'}")
    print(f"Grid mobile styles:  {'PASS' if grid_has_mobile else 'FAIL'}")
    print(f"Text tablet styles:  {'PASS' if text_has_tablet else 'FAIL'}")
    print(f"Text mobile styles:  {'PASS' if text_has_mobile else 'FAIL'}")
    print(f"Image tablet styles: {'PASS' if image_has_tablet else 'FAIL'}")
    print(f"Image mobile styles: {'PASS' if image_has_mobile else 'FAIL'}")
    print(f"\nOverall: {'ALL TESTS PASS' if all_pass else 'SOME TESTS FAILED'}")

    return all_pass


if __name__ == "__main__":
    success = asyncio.run(test_responsive_styles())
    sys.exit(0 if success else 1)
