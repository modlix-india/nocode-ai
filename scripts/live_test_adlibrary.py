"""Live smoke test for the adlibrary.com integration (Tier 1 — API only).

Hits the real adlibrary.com /api/search and runs our normalizer on the response.
Does NOT touch the gateway/storage, so it only needs the API key.

Setup:
    add  ADLIBRARY_API_KEY=adl_...  to .env  (or export it)

Usage:
    python scripts/live_test_adlibrary.py [domain] [name]
    python scripts/live_test_adlibrary.py gymshark.com Gymshark

It prints the RAW field names the API actually returns (so we can confirm our
mapping matches reality — docs may differ) plus the normalized creative record.
"""

import asyncio
import json
import os
import sys

# Allow running directly (python scripts/live_test_adlibrary.py): put repo root on path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.agents.adzump.services import adlibrary_client as adlib


async def main() -> None:
    # Pull config (adzump.adLibraryAPIKey) from the Config Server, same as the service.
    try:
        from app.services.config_server import initialize_config_from_server
        cfg = await initialize_config_from_server()
        settings.apply_config_server_values(cfg)
    except Exception as e:
        print("config-server load skipped:", e)

    if not settings.ADLIBRARY_API_KEY:
        print("ADLIBRARY_API_KEY still empty (config server + env both unset).")
        return
    print(f"key loaded ✓  base={settings.ADLIBRARY_BASE_URL}")

    domain = sys.argv[1] if len(sys.argv) > 1 else "gymshark.com"
    name = sys.argv[2] if len(sys.argv) > 2 else "Gymshark"
    print(f"query: name={name!r}  (domain-narrow={domain!r})")

    # Raw keyword page first — inspect the real field names vs our normalizer.
    try:
        page = await adlib.search_ads(keyword=name, page_size=5)
    except adlib.AdLibraryError as e:
        print("AdLibraryError:", e)
        return
    results = page.get("results") or []
    print(f"\ntotal={page.get('total')}  returned={len(results)}  credits={page.get('_credits')}")
    if results:
        print("RAW ad[0] keys:", sorted(results[0].keys()))

    # Full path: keyword fetch + domain narrowing + normalize.
    ads = await adlib.fetch_competitor_ads(domain=domain, name=name, max_results=5)
    print(f"\nafter domain-narrow: {len(ads)} ads")
    if not ads:
        print("No ads matched — try another brand/domain.")
        return
    rec = adlib.build_library_record(domain=domain, name=name, raw_ads=ads)
    print(f"competitorKey={rec['competitorKey']}  total={rec['totalCreatives']}  "
          f"active={rec['activeCreatives']}  status={rec['fetchStatus']}")
    print("\nfirst normalized creative:")
    print(json.dumps(rec["creatives"][0], indent=2))


if __name__ == "__main__":
    asyncio.run(main())
