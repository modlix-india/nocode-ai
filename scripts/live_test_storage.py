"""Tier-2 live test: creative-library storage round-trip via the gateway.

Exercises the real backend: get_competitor (miss) -> fetch from the source +
rehost binaries -> upsert -> get_competitor (hit).

Needs a logged-in session's auth (kept out of the file - pass via env) and the
CompetitorCreativeLibrary storage to exist for the client:

    export AI_TEST_JWT='<bearer token, no "Bearer " prefix>'
    export AI_TEST_CLIENT_CODE='<clientCode>'
    python scripts/live_test_storage.py [name] [domain]
    # default: Gymshark gymshark.com
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.services.config_server import initialize_config_from_server
from app.agents.adzump import creative_intelligence as ci
from app.agents.adzump.creative_intelligence import store


async def main() -> None:
    try:
        settings.apply_config_server_values(await initialize_config_from_server())
    except Exception as e:
        print("config-server load skipped:", e)

    jwt = os.getenv("AI_TEST_JWT")
    client_code = os.getenv("AI_TEST_CLIENT_CODE")
    if not jwt or not client_code:
        print("Set AI_TEST_JWT and AI_TEST_CLIENT_CODE first.")
        return
    if not settings.ADLIBRARY_API_KEY:
        print("ADLIBRARY_API_KEY not loaded (config server).")
        return

    name = sys.argv[1] if len(sys.argv) > 1 else "Gymshark"
    domain = sys.argv[2] if len(sys.argv) > 2 else "gymshark.com"
    key = ci.competitor_key(domain)
    ctx = {
        "headers": {"Authorization": f"Bearer {jwt}", "appCode": store.APP_CODE},
        "client_code": client_code,
        "session_context": {},
    }
    print(f"gateway={settings.GATEWAY_URL}  clientCode={client_code}  "
          f"shared={settings.CREATIVE_LIBRARY_SHARED}  key={key}")

    before = await store.get_competitor(key, ctx)
    print(f"\n1) get_competitor (before): {'HIT' if before else 'miss'}")

    print("2) fetch + rehost + upsert (force) ...")
    rec = await ci.creatives_for(key=key, name=name, ctx=ctx, force=True)
    if not rec:
        print("   creatives_for returned None - check source/auth.")
        return
    rehosted = sum(1 for c in rec.creatives if c.file_url or c.poster_url)
    print(f"   stored: total={rec.total_creatives} active={rec.active_creatives} "
          f"rehosted_binaries={rehosted}")
    for c in rec.creatives:
        u = c.file_url or c.poster_url
        if u:
            print("   sample rehosted url:", u)
            break

    after = await store.get_competitor(key, ctx)
    print(f"\n3) get_competitor (after): {'HIT - round-trip OK' if after else 'MISS - write failed'}")
    if after:
        print(f"   stored competitorKey={after.competitor_key} total={after.total_creatives} "
              f"lastFetchedAt={after.last_fetched_at}")


if __name__ == "__main__":
    asyncio.run(main())
