#!/usr/bin/env python3
"""Generate the UIEngine.* function catalog from nocode-ui source.

Writes app/agents/appbuilder/tools/modlix/_uiengine_catalog.py from the
FunctionSignature blocks in nocode-ui/ui-app/client/src/functions/*.ts, using
all.ts as the authoritative list of what the browser runtime actually exports.

Why generated: the previous hand-written UIENGINE_PRIMITIVES set carried 11
names that never existed (Read, Create, Update, Delete, GetStore, OpenModal,
CloseModal, Reload, SetCookies, GetCookies, ObjectEntries) and omitted 17 real
ones, FetchData among them. The agent was shown the fake list, asked for
UIEngine.Read, got null, and fell back to SetStore mock data for every page.

Usage:
    python scripts/gen_uiengine_catalog.py [--ui-root /path/to/nocode-ui]

Re-run whenever nocode-ui adds or changes a UIEngine function. The drift test
tests/test_tooling_fixes_chitfund_audit.py::test_uiengine_catalog_matches_nocode_ui_checkout
fails when the generated module and all.ts disagree (skipped when the
nocode-ui checkout is not present).
"""
from __future__ import annotations

import argparse
import pprint
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
OUT = REPO / "app" / "agents" / "appbuilder" / "tools" / "modlix" / "_uiengine_catalog.py"
DEFAULT_UI_ROOT = REPO.parent / "nocode-ui"
FUNCTIONS_REL = Path("ui-app/client/src/functions")

_SCHEMA_TYPES = {
    "ofString": "string",
    "ofBoolean": "boolean",
    "ofNumber": "number",
    "ofInteger": "integer",
    "ofAny": "any",
    "ofObject": "object",
    "ofArray": "array",
}


def _js_unescape(s: str) -> str:
    return (
        s.replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\'", "'")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )


def _exported_names(all_ts: str) -> list[str]:
    """Function class names all.ts imports from sibling files (`from './X'`)."""
    return sorted(set(re.findall(r"from\s+'\./(\w+)'", all_ts)))


def _split_param_blocks(sig_src: str) -> list[str]:
    """Return the text of each Parameter.ofEntry(...) call, balanced on parens."""
    blocks: list[str] = []
    for m in re.finditer(r"Parameter\.ofEntry\(", sig_src):
        depth, i = 0, m.end() - 1
        while i < len(sig_src):
            ch = sig_src[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        blocks.append(sig_src[m.end(): i])
    return blocks


def _parse_param(block: str) -> tuple[str, dict]:
    name = re.match(r"\s*'(\w+)'", block).group(1)
    schema = re.search(r"Schema\.(\w+)\(([^)]*)\)", block)
    kind = schema.group(1) if schema else "?"
    info: dict = {"type": _SCHEMA_TYPES.get(kind, kind)}
    if kind == "ofRef":
        ref = schema.group(2).strip().strip("`'\"")
        info["type"] = "ref"
        info["ref"] = ref.replace("${NAMESPACE_UI_ENGINE}", "UIEngine")
    if ".setDefaultValue(" in block:
        info["hasDefault"] = True
    return name, info


def _parse_events(sig_src: str) -> dict[str, list[str]]:
    events: dict[str, list[str]] = {}
    for m in re.finditer(r"Event\.eventMapEntry\(\s*Event\.(\w+)\s*,\s*new Map\(\[(.*?)\]\)\s*,?\s*\)", sig_src, re.S):
        events[m.group(1).lower()] = re.findall(r"\[\s*'(\w+)'", m.group(2))
    return events


def _parse_signature(src: str) -> dict | None:
    if "NAMESPACE_UI_ENGINE" not in src:
        return None
    # The builder chain runs from `new FunctionSignature('X')` up to the class
    # declaration that follows it; files differ in how the chain is terminated
    # (`;` on its own line vs. trailing `);`), so anchor on the next `export`.
    sig = re.search(r"new FunctionSignature\('(\w+)'\)(.*?)(?=\n\s*export\s)", src, re.S)
    if not sig:
        return None
    body = sig.group(2)
    params = dict(_parse_param(b) for b in _split_param_blocks(body))
    desc = re.search(r"\.setDescription\(\s*'((?:[^'\\]|\\.)*)'", body)
    doc = re.search(r"\.setDocumentation\(\s*'((?:[^'\\]|\\.)*)'", body)
    return {
        "name": sig.group(1),
        "parameters": params,
        "events": _parse_events(body),
        "description": _js_unescape(desc.group(1)) if desc else "",
        "documentation": _js_unescape(doc.group(1)) if doc else "",
    }


def build_catalog(ui_root: Path) -> dict[str, dict]:
    fn_dir = ui_root / FUNCTIONS_REL
    all_ts = (fn_dir / "all.ts").read_text(encoding="utf-8")
    catalog: dict[str, dict] = {}
    for name in _exported_names(all_ts):
        path = fn_dir / f"{name}.ts"
        if not path.exists():
            print(f"warning: {name} imported by all.ts but {path.name} missing", file=sys.stderr)
            continue
        parsed = _parse_signature(path.read_text(encoding="utf-8"))
        if parsed is None:
            print(f"skip {name}: not a NAMESPACE_UI_ENGINE FunctionSignature", file=sys.stderr)
            continue
        parsed["source"] = str(FUNCTIONS_REL / f"{name}.ts")
        catalog[parsed["name"]] = parsed
    return catalog


def render_module(catalog: dict[str, dict]) -> str:
    lines = [
        '"""GENERATED by scripts/gen_uiengine_catalog.py — do not edit by hand.',
        "",
        "Signatures of the UIEngine.* functions the browser-side Kirun runtime",
        "exports (nocode-ui/ui-app/client/src/functions/all.ts). Consumed by",
        "_conventions.UIENGINE_PRIMITIVES (name set) and get_kirun_primitive",
        "(signature lookup). Re-generate when nocode-ui changes.",
        '"""',
        "from __future__ import annotations",
        "",
        "from typing import Any",
        "",
        "UIENGINE_SIGNATURES: dict[str, dict[str, Any]] = {",
    ]
    for name in sorted(catalog):
        entry = {k: v for k, v in catalog[name].items() if k != "name"}
        # pformat, not json.dumps: the module must be valid Python (True, not true).
        rendered = pprint.pformat(entry, width=100, sort_dicts=True)
        indented = "\n".join(("    " + ln) if i else ln for i, ln in enumerate(rendered.splitlines()))
        lines.append(f"    {name!r}: {indented},")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ui-root", type=Path, default=DEFAULT_UI_ROOT)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    if not (args.ui_root / FUNCTIONS_REL / "all.ts").exists():
        sys.exit(f"nocode-ui functions not found under {args.ui_root}")
    catalog = build_catalog(args.ui_root)
    args.out.write_text(render_module(catalog), encoding="utf-8")
    print(f"wrote {args.out} with {len(catalog)} UIEngine functions: {', '.join(sorted(catalog))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
