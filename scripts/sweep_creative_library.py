"""CLI for the CompetitorCreativeLibrary repair sweep (Rule 9).

Verifies every stored creative's fileUrl/posterUrl through the served public
path and removes the ones that no longer render (404, html body, 0-byte,
undecodable), appending diagnostics to the record's dropped[] array.

Schedule WEEKLY (static files can be deleted or paths migrated):
    0 3 * * 1  cd <repo> && venv/bin/python scripts/sweep_creative_library.py

Auth: the storage API needs a bearer token + client scope. Provide:
    SWEEP_TOKEN        bearer token of any user in the library's client scope
    SWEEP_CLIENT_CODE  clientCode to sweep under (default: the shared scope)

Usage:
    source ~/.nocode-ai/variables.sh
    python scripts/sweep_creative_library.py [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.adzump.creative_intelligence.sweep import sweep_library


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be removed; write nothing")
    args = parser.parse_args()

    token = os.environ.get("SWEEP_TOKEN", "")
    if not token:
        raise SystemExit("SWEEP_TOKEN is required (bearer token for the "
                         "storage API) - see the module docstring.")
    ctx = {
        "headers": {"Authorization": token},
        "client_code": os.environ.get("SWEEP_CLIENT_CODE", ""),
    }
    report = asyncio.run(sweep_library(ctx, dry_run=args.dry_run))
    print(json.dumps(report, indent=1))
    if report["removed_by_reason"]:
        print("\nseed any placeholder md5s found into "
              "verify._KNOWN_PLACEHOLDER_MD5S")


if __name__ == "__main__":
    main()
