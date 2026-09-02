#!/usr/bin/env python3
"""Build a KIRun function corpus from the definitions on disk, and census it.

Why: the appbuilder's existing pattern corpus holds 629 hand-picked `.dsl`
files (512 browser-only, 57 server-only). The `definitions/` dump already
contains **7,497 page event functions** across 364 pages, 15x the browser
sample, and nobody has looked at them. Page functions live INSIDE the page
document under `eventFunctions`, which is why they were never collected
separately.

Shape of the source data:

    page.eventFunctions[<fnKey>] = {
      name: "footer_form",
      steps: {
        <statementName>: {
          statementName, name, namespace,
          parameterMap: { <param>: { <slotKey>: {type: VALUE|EXPRESSION,
                                                 value?, expression?, order} } },
          dependentStatements: { "Steps.<other>.<event>": true },
          position: {left, top},        # dropped: canvas layout, not logic
        }
      }
    }

What this writes (JSONL, one function per line) keeps the logic and discards the
canvas: namespace-qualified step calls, the parameter slots reduced to
(kind, value) pairs, and the dependency edges. That is the part a compiler or a
training set cares about; `position` is noise that would dominate any diff.

Server (backend) functions are NOT here — `definitions/` contains zero of them
(they are separate Function documents, not embedded in pages). Pass
`--gateway`/`--app-code` to pull those, and see the WARNING in `_fetch_server`:
it reads from whatever environment you point it at.

Usage:
    ./venv/bin/python scripts/build_function_corpus.py                  # census only
    ./venv/bin/python scripts/build_function_corpus.py --out corpus/    # write JSONL
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import statistics
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))


def _slot(slots: dict) -> list[dict]:
    """Reduce one parameter's slot map to an ordered list of (kind, value).

    A parameter is a map of slotKey -> {type, value|expression, order}; the keys
    are random ids, so they are dropped and `order` decides the sequence.
    """
    out = []
    for spec in sorted(
        (s for s in slots.values() if isinstance(s, dict)),
        key=lambda s: s.get("order") or 0,
    ):
        kind = spec.get("type") or "VALUE"
        if not isinstance(kind, str):
            # Seen in prod: a `type` that is itself a structure. Keep the record
            # faithful but give the census something hashable to count.
            kind = type(kind).__name__.upper()
        val = spec.get("expression") if kind == "EXPRESSION" else spec.get("value")
        out.append({"kind": kind, "value": val})
    return out


def _steps(fn: dict) -> list[dict]:
    """Normalise a function's step map into a list, canvas position dropped."""
    raw = fn.get("steps")
    if not isinstance(raw, dict):
        return []
    steps = []
    for stmt, s in raw.items():
        if not isinstance(s, dict):
            continue
        params = {}
        pm = s.get("parameterMap")
        if isinstance(pm, dict):
            for pname, slots in pm.items():
                if isinstance(slots, dict):
                    params[pname] = _slot(slots)
        deps = sorted(k for k, v in (s.get("dependentStatements") or {}).items() if v)
        steps.append({
            "statement": s.get("statementName") or stmt,
            "call": f"{s.get('namespace')}.{s.get('name')}",
            "params": params,
            "deps": deps,
        })
    steps.sort(key=lambda x: x["statement"])
    return steps


def collect_page_functions(root: str) -> list[dict]:
    """Every page event function in the definitions dump, normalised."""
    out = []
    for path in glob.glob(os.path.join(root, "**", "*.json"), recursive=True):
        try:
            with open(path) as fh:
                doc = json.load(fh)
        except (ValueError, OSError):
            continue
        for d in (doc if isinstance(doc, list) else [doc]):
            if not isinstance(d, dict) or "componentDefinition" not in d:
                continue
            efs = d.get("eventFunctions")
            if not isinstance(efs, dict):
                continue
            for fn_key, fn in efs.items():
                if not isinstance(fn, dict):
                    continue
                steps = _steps(fn)
                if not steps:
                    continue
                out.append({
                    "surface": "page",
                    "app_code": d.get("appCode"),
                    "client_code": d.get("clientCode"),
                    "page": d.get("name"),
                    "fn_key": fn_key,
                    "fn_name": fn.get("name"),
                    "n_steps": len(steps),
                    "steps": steps,
                })
    return out


def _sig(defn: dict, key: str) -> dict:
    """Declared `parameters` / `events` of a standalone function, names + types.

    Page event functions have no signature — they are handlers, invoked by the
    component that owns them. Standalone `ui.function` / `core.function`
    documents do, and it is the part a caller has to satisfy.
    """
    raw = defn.get(key)
    if not isinstance(raw, dict):
        return {}
    out = {}
    for pname, spec in raw.items():
        if isinstance(spec, dict):
            schema = spec.get("schema") if isinstance(spec.get("schema"), dict) else spec
            out[pname] = schema.get("type") or schema.get("ref") or "?"
    return out


def collect_standalone(path: str, surface: str) -> list[dict]:
    """Normalise a mongoexport of `ui.function` or `core.function`.

    Same step shape as page event functions (statementName / name / namespace /
    parameterMap / dependentStatements), so `_steps` is shared. The differences
    are that `name` is already namespace-qualified ("ZohoFunctions.fetchDocument
    Details") and the body sits under `definition` rather than at the top level.
    """
    out = []
    if not os.path.exists(path):
        return out
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            defn = d.get("definition")
            if not isinstance(defn, dict):
                continue
            steps = _steps(defn)
            if not steps:
                continue
            out.append({
                "surface": surface,
                "app_code": d.get("appCode"),
                "client_code": d.get("clientCode"),
                "page": None,
                "fn_key": None,
                "fn_name": d.get("name"),
                "params": _sig(defn, "parameters"),
                "events": _sig(defn, "events"),
                "n_steps": len(steps),
                "steps": steps,
            })
    return out


def collect_exported_pages(path: str) -> list[dict]:
    """Page event functions from a projected mongoexport of `ui.page`.

    Same extraction as `collect_page_functions`, but from the export rather than
    the on-disk dump — the export carries 1,781 pages against the dump's 398, so
    this is the wider sample of the same thing.
    """
    out = []
    if not os.path.exists(path):
        return out
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            efs = d.get("eventFunctions")
            if not isinstance(efs, dict):
                continue
            for fn_key, fn in efs.items():
                if not isinstance(fn, dict):
                    continue
                steps = _steps(fn)
                if not steps:
                    continue
                out.append({
                    "surface": "page",
                    "app_code": d.get("appCode"),
                    "client_code": d.get("clientCode"),
                    "page": d.get("name"),
                    "fn_key": fn_key,
                    "fn_name": fn.get("name"),
                    "n_steps": len(steps),
                    "steps": steps,
                })
    return out


def census(records: list[dict]) -> None:
    """What is actually in here, before deciding what to do with it."""
    if not records:
        print("no functions found")
        return
    calls = collections.Counter()
    per_fn = []
    param_kinds = collections.Counter()
    dep_counts = collections.Counter()
    local_calls = 0
    for r in records:
        per_fn.append(r["n_steps"])
        for s in r["steps"]:
            calls[s["call"]] += 1
            if s["call"].startswith("_."):
                local_calls += 1
            for slots in s["params"].values():
                for slot in slots:
                    param_kinds[slot["kind"]] += 1
            dep_counts[len(s["deps"])] += 1

    total_steps = sum(per_fn)
    print(f"functions: {len(records):,}   steps: {total_steps:,}")
    print(f"steps per function: median {statistics.median(per_fn):.0f}, "
          f"mean {statistics.mean(per_fn):.1f}, max {max(per_fn)}")
    trivial = sum(1 for n in per_fn if n == 1)
    print(f"single-step functions: {trivial:,} ({trivial / len(records) * 100:.0f}%)")
    print(f"distinct step calls: {len(calls):,}")
    need = 0
    running = 0
    for _, n in calls.most_common():
        running += n
        need += 1
        if running >= total_steps * 0.8:
            break
    print(f"distinct calls covering 80% of steps: {need}")
    print(f"app-local (`_.fn`) call sites: {local_calls:,} "
          f"({local_calls / total_steps * 100:.0f}% of steps)")
    print(f"parameter slot kinds: {dict(param_kinds)}")
    print(f"steps by dependency count: "
          f"{dict(sorted(dep_counts.items())[:6])}")
    print("\ntop step calls:")
    for c, n in calls.most_common(15):
        print(f"  {n:6d}  {c}")
    print("\nby app:")
    by_app = collections.Counter(r["app_code"] for r in records)
    for app, n in by_app.most_common(10):
        print(f"  {n:5d}  {app}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="definitions", help="Definitions dump root")
    ap.add_argument("--raw", default=None,
                    help="Directory of mongoexport JSONL (ui_page_eventfns / "
                         "ui_function / core_function). Wider than --root.")
    ap.add_argument("--out", default=None,
                    help="Directory to write page_functions.jsonl into")
    args = ap.parse_args()

    if args.raw:
        raw = Path(args.raw)
        records = (
            collect_exported_pages(str(raw / "ui_page_eventfns.jsonl"))
            + collect_standalone(str(raw / "ui_function.jsonl"), "ui")
            + collect_standalone(str(raw / "core_function.jsonl"), "core")
        )
    else:
        records = collect_page_functions(args.root)

    by_surface = collections.Counter(r["surface"] for r in records)
    print("by surface:", dict(by_surface))
    for surface in ("page", "ui", "core"):
        subset = [r for r in records if r["surface"] == surface]
        if subset:
            print(f"\n{'=' * 58}\n{surface.upper()}\n{'=' * 58}")
            census(subset)
    if len(by_surface) > 1:
        print(f"\n{'=' * 58}\nALL SURFACES\n{'=' * 58}")
    census(records)

    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / ("functions.jsonl" if args.raw else "page_functions.jsonl")
        with open(path, "w") as fh:
            for r in records:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        size = path.stat().st_size
        print(f"\nwrote {len(records):,} records to {path} ({size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
