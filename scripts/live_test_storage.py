"""Tier-2 live test: per-client creative-library storage round-trip via the gateway.

Exercises the real backend: get_competitor (miss) -> fetch from adlibrary +
rehost binaries -> upsert -> get_competitor (hit).

Needs a logged-in session's auth (kept out of the file — pass via env) and the
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
from app.agents.adzump.services import creative_library as lib
from app.agents.adzump.services import competitor_creatives as cc


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
    key = lib.competitor_key(domain)
    ctx = {
        "headers": {"Authorization": f"Bearer {jwt}", "appCode": lib.APP_CODE},
        "client_code": client_code,
        "session_context": {},
    }
    print(f"gateway={settings.GATEWAY_URL}  clientCode={client_code}  "
          f"shared={settings.CREATIVE_LIBRARY_SHARED}  key={key}")

    before = await lib.get_competitor(key, ctx)
    print(f"\n1) get_competitor (before): {'HIT' if before else 'miss'}")

    print("2) fetch + rehost + upsert (force) ...")
    rec = await cc.fetch_for_competitor(key=key, name=name, ctx=ctx, force=True)
    if not rec:
        print("   fetch_for_competitor returned None — check adlibrary/auth.")
        return
    rehosted = sum(1 for c in rec.get("creatives", []) if c.get("fileUrl") or c.get("posterUrl"))
    print(f"   stored: total={rec.get('totalCreatives')} active={rec.get('activeCreatives')} "
          f"rehosted_binaries={rehosted}")
    for c in rec.get("creatives", []):
        u = c.get("fileUrl") or c.get("posterUrl")
        if u:
            print("   sample rehosted url:", u)
            break

    after = await lib.get_competitor(key, ctx)
    print(f"\n3) get_competitor (after): {'HIT — round-trip OK' if after else 'MISS — write failed'}")
    if after:
        d = after.get("data") or after
        print(f"   stored competitorKey={d.get('competitorKey')} total={d.get('totalCreatives')} "
              f"lastFetchedAt={d.get('lastFetchedAt')}")


if __name__ == "__main__":
    asyncio.run(main())
