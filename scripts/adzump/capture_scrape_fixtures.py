"""One-off: capture real scrape inputs for the adzump fixtures. Runs _fetch_page
on real-estate URLs → {url,html,network,image_positions} into
tests/agents/adzump/fixtures/scrape/<name>.json (frozen; suite replays, no live net).
Run (needs live net + playwright):
    cd nocode-ai && PYTHONPATH=. venv/bin/python scripts/adzump/capture_scrape_fixtures.py"""

import asyncio
import json
import os

from app.agents.adzump.agents.product.adapters.playwright_adapter import _fetch_page

SITES = {
    "cityville":      "https://cityville.in/",                                              # 107 img / 76 unique — dup-logo, no-truncate
    "purvasparkling": "https://purvasparklingspring.com/",                                  # svg dev logo — svg-drop
    "godrej_woods":   "https://www.godrejproperties.com/landing-page/bangalore/residential/godrej-woods/",  # heavy/JS-loaded — network merge
    "sumadhura":      "https://sumadhuraepitome.com/",                                       # big gallery — cap
}

OUT_DIR = "tests/agents/adzump/fixtures/scrape"


async def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    for name, url in SITES.items():
        try:
            html, _screenshot, network, image_positions = await _fetch_page(url)
        except Exception as e:
            print(f"FAIL {name} ({url}): {type(e).__name__}: {str(e)[:160]}")
            continue
        path = os.path.join(OUT_DIR, f"{name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "url": url,
                "html": html,
                "network": network,
                "image_positions": image_positions,
            }, f)
        print(f"OK   {name}: html={len(html):>7}B  network={len(network):>3}  → {path}")


if __name__ == "__main__":
    asyncio.run(main())
