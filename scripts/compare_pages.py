#!/usr/bin/env python3
"""
Compare original website with cloned page by taking screenshots.
"""
import asyncio
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright


async def take_screenshots(original_url: str, cloned_url: str, output_dir: Path):
    """Take screenshots of both pages at different viewports."""
    output_dir.mkdir(exist_ok=True)

    viewports = [
        ("desktop", 1440, 900),
        ("tablet", 768, 1024),
        ("mobile", 375, 812),
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch()

        for name, width, height in viewports:
            print(f"\nCapturing {name} ({width}x{height})...")

            # Original page
            context = await browser.new_context(viewport={"width": width, "height": height})
            page = await context.new_page()

            print(f"  Loading original: {original_url}")
            try:
                await page.goto(original_url, wait_until="networkidle", timeout=30000)
                await page.wait_for_timeout(2000)  # Wait for animations

                original_path = output_dir / f"original_{name}.png"
                await page.screenshot(path=str(original_path), full_page=True)
                print(f"  Saved: {original_path}")
            except Exception as e:
                print(f"  ERROR capturing original: {e}")

            await context.close()

            # Cloned page
            context = await browser.new_context(viewport={"width": width, "height": height})
            page = await context.new_page()

            print(f"  Loading cloned: {cloned_url}")
            try:
                await page.goto(cloned_url, wait_until="networkidle", timeout=30000)
                await page.wait_for_timeout(2000)

                cloned_path = output_dir / f"cloned_{name}.png"
                await page.screenshot(path=str(cloned_path), full_page=True)
                print(f"  Saved: {cloned_path}")
            except Exception as e:
                print(f"  ERROR capturing cloned: {e}")

            await context.close()

        await browser.close()

    print(f"\nScreenshots saved to: {output_dir}")


async def main():
    original_url = "https://ceo.pronexus.in/"
    # Correct URL format: /{appCode}/{clientCode}/page/{pageName}
    cloned_url = "https://apps.dev.modlix.com/aitestapp/SYSTEM/page/devTestPage"

    output_dir = Path(__file__).parent / "comparison_screenshots"

    await take_screenshots(original_url, cloned_url, output_dir)


if __name__ == "__main__":
    asyncio.run(main())
