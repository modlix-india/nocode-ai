#!/usr/bin/env python3
"""Measure how much of a page is RECOMBINATION of shapes the corpus already has.

The SLM question turns on this. A fine-tuned compiler is worth its training and
serving cost only if generating a page needs genuine novelty. If a held-out page
is almost entirely made of component shapes that already appear elsewhere in the
corpus, then retrieval over a template library plus the existing LLM filling
slots gets most of the benefit for none of the ML cost — and that is the version
to build first.

Method: reduce every component to a STRUCTURAL SIGNATURE (type + the set of
property keys + child count) — deliberately ignoring values, since values are
what the caller supplies and shapes are what a compiler has to know. Then hold
each page out in turn and ask what fraction of its components have that exact
signature somewhere in the remaining corpus.

Coverage here is an upper bound on what retrieval buys: matching a shape is not
the same as knowing which shape to pick or how to fill it. Read a high number as
"the output language is small", not as "retrieval solves it".

Usage:
    ./venv/bin/python scripts/analyze_definition_corpus.py [--root definitions]
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import statistics
import sys


def signature(c: dict) -> tuple:
    """Type + sorted property keys + child count. Values deliberately excluded."""
    props = c.get("properties")
    keys = tuple(sorted(props.keys())) if isinstance(props, dict) else ()
    children = c.get("children")
    n = len(children) if isinstance(children, dict) else 0
    return (c.get("type"), keys, n)


def load_pages(root: str) -> list[tuple[str, list[tuple]]]:
    """[(page_label, [signature, ...]), ...] for every page that has a tree."""
    pages: list[tuple[str, list[tuple]]] = []
    for path in glob.glob(os.path.join(root, "**", "*.json"), recursive=True):
        try:
            with open(path) as fh:
                doc = json.load(fh)
        except (ValueError, OSError):
            continue
        for d in (doc if isinstance(doc, list) else [doc]):
            if not isinstance(d, dict):
                continue
            cd = d.get("componentDefinition")
            if not isinstance(cd, dict) or not cd:
                continue
            sigs = [signature(c) for c in cd.values() if isinstance(c, dict)]
            if sigs:
                label = f"{d.get('appCode', '?')}/{d.get('name', os.path.basename(path))}"
                pages.append((label, sigs))
    return pages


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="definitions")
    ap.add_argument("--show", type=int, default=12, help="How many worst-covered pages to list")
    ap.add_argument("--dsl-root", default="app/agents/appbuilder/aicontext/patterns",
                    help="Root of .dsl files for the logic report; empty to skip")
    args = ap.parse_args()

    pages = load_pages(args.root)
    if not pages:
        print(f"no pages with a componentDefinition under {args.root!r}")
        return 1

    # Corpus-wide count per signature, so holding a page out is a subtraction
    # rather than a rebuild of the whole index per page.
    total = collections.Counter()
    for _, sigs in pages:
        total.update(sigs)

    rows = []
    for label, sigs in pages:
        held = collections.Counter(sigs)
        covered = sum(n for s, n in held.items() if total[s] - n > 0)
        rows.append((covered / len(sigs), label, len(sigs)))

    cov = [r[0] for r in rows]
    comps = sum(r[2] for r in rows)
    print(f"pages: {len(pages)}   components: {comps:,}   distinct signatures: {len(total):,}")
    print(f"components per page: median {statistics.median(r[2] for r in rows):.0f}, "
          f"mean {comps / len(rows):.0f}")
    print()
    print("Held-out coverage — share of a page's components whose exact shape")
    print("exists elsewhere in the corpus:")
    print(f"  mean   {statistics.mean(cov) * 100:5.1f}%")
    print(f"  median {statistics.median(cov) * 100:5.1f}%")
    for pct in (10, 25, 50, 75, 90):
        q = sorted(cov)[min(len(cov) - 1, int(len(cov) * pct / 100))]
        print(f"  p{pct:<2d}    {q * 100:5.1f}%")
    print(f"  pages ≥95% covered: {sum(1 for c in cov if c >= 0.95)}/{len(cov)}")
    print(f"  pages <50% covered: {sum(1 for c in cov if c < 0.50)}/{len(cov)}")
    print()

    # How concentrated is the shape vocabulary? If a small head covers most
    # components, the template library is small enough to hand-curate.
    ordered = total.most_common()
    running = 0
    marks = {}
    for i, (_, n) in enumerate(ordered, 1):
        running += n
        for target in (50, 80, 90, 95, 99):
            if target not in marks and running >= comps * target / 100:
                marks[target] = i
    print("Shape concentration — distinct signatures needed to cover N% of all components:")
    for target in (50, 80, 90, 95, 99):
        print(f"  {target}%: {marks.get(target, len(ordered)):,} signatures")
    print()

    rows.sort()
    print(f"Least-covered pages (the ones a template library would NOT carry):")
    for c, label, n in rows[:args.show]:
        print(f"  {c * 100:5.1f}%  {n:4d} comps  {label}")

    _style_report(args.root)
    if args.dsl_root:
        _dsl_report(args.dsl_root)
    return 0


def style_leaves(c: dict):
    """Yield (property_name, value) for every style leaf on a component.

    styleProperties is {rule_uuid: {resolutions: {BREAKPOINT: {prop: {value}}}}}.
    The uuid and breakpoint are addressing, not vocabulary, so both are dropped.
    """
    sp = c.get("styleProperties")
    if not isinstance(sp, dict):
        return
    for rule in sp.values():
        if not isinstance(rule, dict):
            continue
        for res in (rule.get("resolutions") or {}).values():
            if not isinstance(res, dict):
                continue
            for prop, spec in res.items():
                if isinstance(spec, dict) and "value" in spec:
                    yield (prop, str(spec["value"]))


def _style_report(root: str) -> None:
    """Same question for STYLING, which the structural signature ignores.

    Component shapes recombine; the visual work may not. A clone task is almost
    entirely styling, so a compiler that knows every shape and no styling has
    solved the easy half. Measured two ways: the property VOCABULARY (which CSS
    properties get set at all) and the full (property, value) pair, which is what
    a template would actually have to carry.
    """
    props = collections.Counter()
    pairs = collections.Counter()
    styled_components = 0
    for path in glob.glob(os.path.join(root, "**", "*.json"), recursive=True):
        try:
            with open(path) as fh:
                doc = json.load(fh)
        except (ValueError, OSError):
            continue
        for d in (doc if isinstance(doc, list) else [doc]):
            if not isinstance(d, dict):
                continue
            cd = d.get("componentDefinition")
            if not isinstance(cd, dict):
                continue
            for c in cd.values():
                if not isinstance(c, dict):
                    continue
                leaves = list(style_leaves(c))
                if leaves:
                    styled_components += 1
                for prop, val in leaves:
                    props[prop] += 1
                    pairs[(prop, val)] += 1

    total_leaves = sum(props.values())
    if not total_leaves:
        return
    print()
    print(f"Styling — {total_leaves:,} style leaves on {styled_components:,} components")
    print(f"  distinct CSS properties used : {len(props):,}")
    print(f"  distinct (property, value)   : {len(pairs):,}")
    singles = sum(1 for _, n in pairs.items() if n == 1)
    print(f"  (property, value) pairs seen exactly ONCE: {singles:,} "
          f"({singles / len(pairs) * 100:.0f}% of distinct pairs, "
          f"{singles / total_leaves * 100:.0f}% of all leaves)")
    running = 0
    for target in (50, 80, 95):
        running, need = 0, 0
        for _, n in pairs.most_common():
            running += n
            need += 1
            if running >= total_leaves * target / 100:
                break
        print(f"  distinct pairs to cover {target}% of leaves: {need:,}")
    print("  most-set properties:", ", ".join(f"{p}({n:,})" for p, n in props.most_common(8)))


_STEP_RE = re.compile(r"^\s*(\w+)\s*:\s*([\w.]+)\s*\(", re.MULTILINE)


def _dsl_report(root: str) -> None:
    """The same question for LOGIC: how novel is a KIRun function?

    Structure recombines and styling largely does not; logic is the third axis
    and the one with the cleanest verifier (the DSL either compiles or it does
    not). If functions are built from a small vocabulary of step calls, the same
    "template library beats a fine-tune" argument applies; if every function
    reaches for something different, generation is doing real work.
    """
    files = sorted(glob.glob(os.path.join(root, "**", "*.dsl"), recursive=True))
    if not files:
        return
    calls = collections.Counter()
    per_file = []
    for path in files:
        try:
            with open(path) as fh:
                text = fh.read()
        except OSError:
            continue
        fns = [fn for _, fn in _STEP_RE.findall(text)]
        if not fns:
            continue
        calls.update(fns)
        per_file.append((os.path.basename(path), fns))
    total = sum(calls.values())
    if not total:
        return
    print()
    print(f"Logic — {len(per_file):,} Kirun functions, {total:,} steps")
    print(f"  distinct step functions called: {len(calls):,}")
    print(f"  steps per function: median {statistics.median(len(f) for _, f in per_file):.0f}, "
          f"max {max(len(f) for _, f in per_file)}")
    running, need = 0, 0
    for _, n in calls.most_common():
        running += n
        need += 1
        if running >= total * 0.8:
            break
    print(f"  distinct step functions to cover 80% of steps: {need}")
    # Held-out: does a function reach for any step nothing else uses?
    novel = 0
    for _, fns in per_file:
        held = collections.Counter(fns)
        if any(calls[f] - n == 0 for f, n in held.items()):
            novel += 1
    print(f"  functions using at least one step no other function uses: "
          f"{novel}/{len(per_file)} ({novel / len(per_file) * 100:.0f}%)")
    print("  most-used steps:", ", ".join(f"{c}({n})" for c, n in calls.most_common(8)))


if __name__ == "__main__":
    sys.exit(main())
