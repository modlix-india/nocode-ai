#!/usr/bin/env python3
"""Regenerate APP_THEME_VARIABLES in tools/modlix/_theme_floor.py.

The set is a copy of the app-level theme variables declared in nocode-ui, which
nocode-ai has no other way to see: the component catalog it loads from the CDN
carries components, not the app style sheet.

    python scripts/gen_theme_vars.py [--nocode-ui PATH] [--check]

`--check` exits non-zero if the copy has drifted, which is what CI wants.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_UI = Path.home() / "kiran/fincity/nocode-ui"
REL_SOURCE = "ui-app/client/src/App/appStyleProperties.ts"
TARGET = (
    Path(__file__).resolve().parent.parent
    / "app/agents/appbuilder/tools/modlix/_theme_floor.py"
)

# `n: 'name'` but not the `gn:`/`dn:` that sit beside it in the same object.
_NAME = re.compile(r"(?<![a-zA-Z])n:\s*'([^']+)'")


def read_variable_names(source: Path) -> list[str]:
    text = source.read_text()
    names: set[str] = set()
    for block in re.finditer(r"\{[^{}]*?\}", text, re.S):
        m = _NAME.search(block.group(0))
        if m:
            names.add(m.group(1))
    return sorted(names)


def render(names: list[str]) -> str:
    lines = []
    for i in range(0, len(names), 4):
        lines.append("    " + " ".join(f"{n!r}," for n in names[i:i + 4]))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nocode-ui", type=Path, default=DEFAULT_UI)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    source = args.nocode_ui / REL_SOURCE
    if not source.exists():
        print(f"Source not found: {source}", file=sys.stderr)
        return 2

    names = read_variable_names(source)
    if not names:
        print(f"Parsed no variables from {source} -- has its shape changed?", file=sys.stderr)
        return 2

    current = TARGET.read_text()
    block = re.search(
        r"(APP_THEME_VARIABLES: frozenset\[str\] = frozenset\(\{\n)(.*?)(\}\))",
        current,
        re.S,
    )
    if not block:
        print("Could not find APP_THEME_VARIABLES in the target.", file=sys.stderr)
        return 2

    existing = sorted(re.findall(r"'([^']+)'", block.group(2)))
    if existing == names:
        print(f"Up to date ({len(names)} variables).")
        return 0

    added = sorted(set(names) - set(existing))
    removed = sorted(set(existing) - set(names))
    print(f"Drift: +{len(added)} -{len(removed)}")
    for n in added:
        print(f"  + {n}")
    for n in removed:
        print(f"  - {n}")

    if args.check:
        return 1

    TARGET.write_text(
        current[: block.start(2)] + render(names) + "\n" + current[block.end(2):]
    )
    print(f"Rewrote {TARGET} with {len(names)} variables.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
