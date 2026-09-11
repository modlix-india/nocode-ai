"""Live smoke test for the adlibrary.com source (Tier 1 - API only).

Hits the real adlibrary.com /api/search through the AdLibrarySource adapter and
prints the typed Creative records it maps out. Does NOT touch the gateway/storage,
so it only needs the API key.

Setup:
    add  ADLIBRARY_API_KEY=adl_...  to .env  (or export it)

Usage:
    python scripts/live_test_adlibrary.py [domain] [name]
    python scripts/live_test_adlibrary.py gymshark.com Gymshark
"""

import asyncio
import json
import os
import sys

# Allow running directly: put repo root on path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.agents.adzump.creative_intelligence.sources.adlibrary import (
    AdLibrarySource,
    AdLibraryError,
)


async def main() -> None:
    # Pull config (adzump.adLibraryAPIKey) from the Config Server, same as the service.
    try:
        from app.services.config_server import initialize_config_from_server
        settings.apply_config_server_values(await initialize_config_from_server())
    except Exception as e:
        print("config-server load skipped:", e)

    if not settings.ADLIBRARY_API_KEY:
        print("ADLIBRARY_API_KEY still empty (config server + env both unset).")
        return
    print(f"key loaded  base={settings.ADLIBRARY_BASE_URL}")

    domain = sys.argv[1] if len(sys.argv) > 1 else "gymshark.com"
    name = sys.argv[2] if len(sys.argv) > 2 else "Gymshark"
    print(f"query: name={name!r}  (domain-narrow={domain!r})")

    try:
        fetched = await AdLibrarySource().fetch(domain=domain, name=name)
    except AdLibraryError as e:
        print("AdLibraryError:", e)
        return

    print(f"\nresolved_name={fetched.resolved_name!r}  logo={bool(fetched.logo_url)}  "
          f"creatives={len(fetched.creatives)}")
    if not fetched.creatives:
        print("No ads matched - try another brand/domain.")
        return
    active = sum(1 for c in fetched.creatives if c.is_active)
    print(f"active={active}")
    print("\nfirst mapped creative:")
    print(json.dumps(fetched.creatives[0].model_dump(by_alias=True), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
