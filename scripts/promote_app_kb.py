#!/usr/bin/env python3
"""Promote a per-app KB from one env to another (e.g. dev → stage → prod).

Companion to scripts/migrate_modlix_apps.py: that one seeds the initial state
from modlix-apps folders; this one keeps the destination env in sync as the
app gets built up on dev. Run it alongside the platform's existing artifact
promotion (pages/functions/storages) so the human-knowledge layer travels
with the artifacts it describes.

Reads env config from a YAML/JSON-ish file (default `~/.cfa-envs.yaml`) or
from CFA_ENVS_FILE / CLI args:

    envs:
      dev:    {url: "https://dev.cfa.example",   admin_token: "..."}
      stage:  {url: "https://stage.cfa.example", admin_token: "..."}
      prod:   {url: "https://prod.cfa.example",  admin_token: "..."}

Direction-of-flow rule: apps flow dev → stage → prod, NOT the reverse.
The script refuses to promote from a "lower" env to a "higher" one unless
`--reverse` is passed (rare hot-fix from prod back to dev).

Usage:
    promote_app_kb.py --from dev --to stage --client SYSTEM --app leadzump
    promote_app_kb.py --from dev --to prod --client SYSTEM --app leadzump --mode overwrite
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx


# Order matters: lower index = "lower" env. Promotion is left-to-right.
_ENV_ORDER = ("dev", "stage", "prod")
_DEFAULT_CONFIG = Path("~/.cfa-envs.yaml").expanduser()


def _load_envs(path: Path) -> dict[str, dict[str, str]]:
    """Read env config. Supports YAML-ish (PyYAML if available) or JSON."""
    if not path.exists():
        raise SystemExit(
            f"Env config not found at {path}. Create one with:\n"
            "    envs:\n"
            "      dev:   {url: 'https://...', admin_token: '...'}\n"
            "      stage: {url: 'https://...', admin_token: '...'}\n"
            "      prod:  {url: 'https://...', admin_token: '...'}\n"
            "Or set CFA_ENVS_FILE to a different path."
        )
    text = path.read_text(encoding="utf-8")
    # Try YAML first if available; fall back to JSON if not.
    try:
        import yaml  # type: ignore[import-not-found]
        data = yaml.safe_load(text)
    except ImportError:
        data = json.loads(text)
    if not isinstance(data, dict) or "envs" not in data:
        raise SystemExit(f"Bad env config at {path}: expected top-level `envs` key.")
    envs = data["envs"]
    if not isinstance(envs, dict):
        raise SystemExit(f"Bad env config at {path}: `envs` must be a map.")
    return envs


def _direction_check(src: str, dst: str, reverse: bool) -> None:
    try:
        si = _ENV_ORDER.index(src)
        di = _ENV_ORDER.index(dst)
    except ValueError:
        # Unknown env names — skip the direction check, trust the caller.
        return
    if di < si and not reverse:
        raise SystemExit(
            f"Refusing to promote 'backwards' from {src}→{dst}. Apps flow "
            f"{' → '.join(_ENV_ORDER)}. Pass --reverse for the rare hot-fix "
            "case where prod is the source of truth."
        )


def _call(env_cfg: dict[str, str], path: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = env_cfg["url"].rstrip("/") + path
    headers = {"X-Admin-Token": env_cfg["admin_token"], "Content-Type": "application/json"}
    resp = httpx.post(url, json=payload, headers=headers, timeout=60.0)
    if resp.status_code >= 400:
        raise SystemExit(f"POST {url} failed: HTTP {resp.status_code}: {resp.text[:400]}")
    return resp.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="src", required=True, help="Source env name (e.g. dev)")
    parser.add_argument("--to", dest="dst", required=True, help="Destination env name (e.g. stage)")
    parser.add_argument("--client", required=True, help="clientCode")
    parser.add_argument("--app", required=True, help="appCode")
    parser.add_argument("--mode", choices=("overwrite", "merge"), default="overwrite")
    parser.add_argument("--note", default="", help="Override the auto-generated promotion note")
    parser.add_argument("--reverse", action="store_true", help="Allow backward promotion (rare hot-fix only)")
    parser.add_argument("--envs-file", default=os.environ.get("CFA_ENVS_FILE") or str(_DEFAULT_CONFIG))
    parser.add_argument("--dry-run", action="store_true", help="Export only; don't write to dest")
    args = parser.parse_args()

    if args.src == args.dst:
        raise SystemExit("--from and --to must differ")
    _direction_check(args.src, args.dst, args.reverse)

    envs = _load_envs(Path(args.envs_file).expanduser())
    for name in (args.src, args.dst):
        if name not in envs:
            raise SystemExit(f"Env '{name}' not in {args.envs_file}. Known: {sorted(envs)}")
        for key in ("url", "admin_token"):
            if not envs[name].get(key):
                raise SystemExit(f"Env '{name}' missing '{key}' in {args.envs_file}")

    # Export from source
    print(f"Exporting {args.client}/{args.app} from {args.src} ({envs[args.src]['url']})...")
    exp = _call(envs[args.src], "/api/ai/admin/app-kb/export", {
        "client_code": args.client, "app_code": args.app,
    })
    row_count = exp.get("row_count", 0)
    if row_count == 0:
        print(f"  Source has 0 rows for {args.client}/{args.app}. Nothing to promote.")
        return 0
    print(f"  Got {row_count} row(s) from {args.src}.")

    if args.dry_run:
        # Print a section/version summary for visibility.
        rows = exp["snapshot"].get("rows") or []
        by_section: dict[str, list[int]] = {}
        for r in rows:
            by_section.setdefault(r["SECTION"], []).append(r["VERSION"])
        print("\nDRY-RUN — would import these sections:")
        for section, versions in sorted(by_section.items()):
            latest = max(versions)
            print(f"  {section:18s} latest v{latest}  ({len(versions)} version(s))")
        return 0

    note = args.note or f"Promoted from {args.src} via promote_app_kb.py"
    print(f"\nImporting into {args.dst} ({envs[args.dst]['url']}) mode={args.mode}...")
    imp = _call(envs[args.dst], "/api/ai/admin/app-kb/import", {
        "client_code": args.client,
        "app_code": args.app,
        "snapshot": exp["snapshot"],
        "mode": args.mode,
        "promotion_note": note,
    })
    counters = imp.get("counters") or {}
    print(f"  Done. {counters}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
