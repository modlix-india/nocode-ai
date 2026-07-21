#!/usr/bin/env python3
"""One-off seed migration: modlix-apps/<app>_<client>/ → cfa_app_kb table.

After this runs, the modlix-apps repo can be archived. The script walks each
`<app>_<client>/` folder, maps the well-known files to the typed sections,
and writes via the same persistence layer the agent uses. It's idempotent:
re-running compares body hashes per section and skips rows already at the
latest body.

Mapping:
  OVERVIEW.md      → section 'overview'
  INVENTORY.md     → section 'inventory'
  CONVENTIONS.md   → section 'conventions'
  ROADMAP.md       → section 'roadmap'
  decisions/*.md   → individual rows in 'decisions_log' (sorted by mtime,
                     so older decisions get lower version numbers and the
                     order in the log matches when they were originally made)
  any other *.md   → reported in the script's output for manual review

Folder name pattern: `<appCode>_<clientCode>` (e.g. `leadzump_SYSTEM`,
`appbuilder_SYSTEM`). The clientCode part is REQUIRED — folders that don't
match the pattern are skipped with a warning.

Usage:
  ./venv/bin/python scripts/migrate_modlix_apps.py \\
      --path /Users/kirangrandhi/kiran/fincity/modlix-apps \\
      --updated-by 1 \\
      --message "Seed migration from modlix-apps @ 2026-06-06"

Pass --dry-run to see what WOULD happen without writing.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

# Make app/ importable when running as a script from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from app.db.connection import init_db_pool, close_db_pool  # noqa: E402
from app.services import app_kb  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


_FOLDER_PATTERN = re.compile(r"^(?P<app>[A-Za-z0-9]+)_(?P<client>[A-Za-z0-9]+)$")

# Top-level .md → section mapping (case-INsensitive on the file part).
_TOP_LEVEL_FILE_MAP: dict[str, str] = {
    "OVERVIEW.md":     "overview",
    "INVENTORY.md":    "inventory",
    "CONVENTIONS.md":  "conventions",
    "ROADMAP.md":      "roadmap",
    "CURRENT_FOCUS.md": "current_focus",  # newer convention; map if present
}


async def _migrate_one_section(
    client_code: str, app_code: str, section: str, body: str,
    updated_by: int, message: str, *, dry_run: bool,
) -> str:
    """Return a one-line summary of what (would have) happened."""
    if dry_run:
        # Dry-run intentionally doesn't touch the DB at all — useful when no
        # connection is configured locally.
        return f"DRY-RUN insert ({client_code}/{app_code}/{section}): body={len(body)} chars"
    current = await app_kb.get_latest(client_code, app_code, section)
    new_hash = app_kb.body_hash(body)
    if current and current.get("BODY_HASH") == new_hash:
        return f"skip ({client_code}/{app_code}/{section}): body unchanged at v{current.get('VERSION')}"
    result = await app_kb.insert_version(
        client_code, app_code, section, body,
        updated_by=updated_by, message=message,
    )
    return f"wrote {client_code}/{app_code}/{section} v{result['version']} ({len(body)} chars)"


async def _migrate_decisions(
    client_code: str, app_code: str, decisions_dir: Path,
    updated_by: int, message: str, *, dry_run: bool,
) -> list[str]:
    """Each decisions/*.md becomes its own decisions_log entry."""
    out: list[str] = []
    files = sorted(
        (p for p in decisions_dir.iterdir() if p.is_file() and p.suffix.lower() == ".md"),
        key=lambda p: p.stat().st_mtime,
    )
    if not files:
        return [f"no decisions/*.md in {decisions_dir}"]
    # Pre-fetch all existing decision hashes so we don't re-insert duplicates.
    existing = await app_kb.list_history(client_code, app_code, "decisions_log", limit=500)
    existing_hashes = {r.get("BODY_HASH") for r in existing}
    for f in files:
        body = f.read_text(encoding="utf-8", errors="replace")
        h = app_kb.body_hash(body)
        if h in existing_hashes:
            out.append(f"skip ({client_code}/{app_code}/decisions:{f.name}): already in log")
            continue
        if dry_run:
            out.append(f"DRY-RUN append ({client_code}/{app_code}/decisions:{f.name}): {len(body)} chars")
            continue
        # Use the file's name in the commit message so audits can locate origin.
        msg = f"{message} — from decisions/{f.name}" if message else f"From decisions/{f.name}"
        await app_kb.insert_version(
            client_code, app_code, "decisions_log", body,
            updated_by=updated_by, message=msg,
        )
        out.append(f"wrote {client_code}/{app_code}/decisions_log:{f.name}")
    return out


async def _migrate_app_folder(
    folder: Path, updated_by: int, message: str, *, dry_run: bool,
) -> tuple[list[str], list[str]]:
    """Migrate one <app>_<client>/ folder. Returns (logs, unhandled_files)."""
    m = _FOLDER_PATTERN.match(folder.name)
    if not m:
        return [f"skip {folder.name}: doesn't match <app>_<client> pattern"], []
    app_code = m.group("app")
    client_code = m.group("client")
    logs: list[str] = [f"=== {client_code}/{app_code} ({folder}) ==="]
    unhandled: list[str] = []

    # Case-insensitive lookup against TOP_LEVEL_FILE_MAP.
    name_to_section_lower = {k.lower(): v for k, v in _TOP_LEVEL_FILE_MAP.items()}
    for child in sorted(folder.iterdir()):
        if child.is_dir():
            if child.name == "decisions":
                logs.extend(await _migrate_decisions(
                    client_code, app_code, child, updated_by, message, dry_run=dry_run,
                ))
            else:
                unhandled.append(f"  unhandled dir: {child.relative_to(folder)}/")
            continue
        if not child.is_file() or child.suffix.lower() != ".md":
            continue
        section = name_to_section_lower.get(child.name.lower())
        if section is None:
            unhandled.append(f"  unhandled file: {child.relative_to(folder)} (no section mapping)")
            continue
        body = child.read_text(encoding="utf-8", errors="replace")
        logs.append(await _migrate_one_section(
            client_code, app_code, section, body, updated_by, message, dry_run=dry_run,
        ))
    return logs, unhandled


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", required=True, help="Path to the modlix-apps repo root")
    parser.add_argument("--updated-by", type=int, default=1, help="userId to stamp on every row (default: 1, admin)")
    parser.add_argument("--message", default="Seed migration from modlix-apps", help="Commit message")
    parser.add_argument("--dry-run", action="store_true", help="Don't write; show what would happen")
    parser.add_argument("--only", help="Optional: migrate only this folder (e.g. 'leadzump_SYSTEM')")
    args = parser.parse_args()

    root = Path(args.path).expanduser()
    if not root.exists() or not root.is_dir():
        log.error("Path %s is not a directory", root)
        return 1

    if not args.dry_run:
        log.info("Initializing DB pool...")
        await init_db_pool()
    else:
        log.info("DRY-RUN: skipping DB init")

    try:
        all_logs, all_unhandled = await _walk_root(
            root, args.only, args.updated_by, args.message, args.dry_run,
        )
        for line in all_logs:
            print(line)
        if all_unhandled:
            print()
            print("Unhandled files / dirs (manual review):")
            for line in all_unhandled:
                print(line)
        return 0
    finally:
        if not args.dry_run:
            await close_db_pool()


async def _walk_root(
    root: Path, only: str | None, updated_by: int, message: str, dry_run: bool,
) -> tuple[list[str], list[str]]:
    """Iterate top-level <app>_<client>/ folders, migrate each."""
    all_logs: list[str] = []
    all_unhandled: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if only and child.name != only:
            continue
        if not _FOLDER_PATTERN.match(child.name):
            continue
        logs, unhandled = await _migrate_app_folder(
            child, updated_by, message, dry_run=dry_run,
        )
        all_logs.extend(logs)
        all_unhandled.extend(unhandled)
    return all_logs, all_unhandled


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
