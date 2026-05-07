
import asyncio
import sys
import os

# Add project root to sys.path
sys.path.append(r"e:\Nocode-AI\nocode-ai")

from app.agents.adzump.agents.product.adapters.playwright_adapter import scrape_page

async def test_real_scrape():
    url = "https://www.apple.com" # Apple is a good test for standard images
    print(f"Testing scrape on: {url}")
    
    result = await scrape_page(url)
    
    if result.success and result.content:
        content = result.content
        print(f"Success! Title: {content.title}")
        print(f"Logo URL found: {content.logo_url}")
        print(f"Total images found: {len(content.images)}")
        
        # Show first 10 images
        for i, img in enumerate(content.images[:10]):
            print(f"  {i+1}. {img.src} (alt: {img.alt})")
    else:
        print(f"Scrape failed: {result.error}")

if __name__ == "__main__":
    asyncio.run(test_real_scrape())
