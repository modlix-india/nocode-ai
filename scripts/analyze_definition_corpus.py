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
    ap.add_argument("--min-uses", type=int, default=5,
                    help="Times a subtree must recur to count as a template")
    ap.add_argument("--min-size", type=int, default=3,
                    help="Components a subtree must have to be worth templating")
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
    _subtree_report(args.root, args.min_uses, args.min_size)
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




# ── Subtree templates ───────────────────────────────────────────────────────


def _pages_with_trees(root: str):
    """Yield (label, componentDefinition) for every page carrying a tree."""
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
            if isinstance(cd, dict) and cd:
                yield f"{d.get('appCode', '?')}/{d.get('name', os.path.basename(path))}", cd


def _roots(cd: dict) -> list[str]:
    """Keys that are nobody's child."""
    childed = set()
    for c in cd.values():
        if isinstance(c, dict) and isinstance(c.get("children"), dict):
            childed.update(k for k, v in c["children"].items() if v)
    return [k for k in cd if k not in childed]


def _subtree_sig(cd: dict, key: str, memo: dict, size: dict) -> str:
    """Canonical signature of the subtree rooted at `key`, and its size.

    Children are sorted so that two sections differing only in the ORDER of
    equivalent siblings still match — a template library wants the shape, not
    the shuffle. Property VALUES are excluded for the same reason values are
    excluded from the component signature: they are what the caller supplies.
    """
    if key in memo:
        return memo[key]
    node = cd.get(key)
    if not isinstance(node, dict):
        memo[key], size[key] = "?", 1
        return "?"
    kids = node.get("children")
    kid_keys = [k for k, v in kids.items() if v and k in cd] if isinstance(kids, dict) else []
    parts = sorted(_subtree_sig(cd, k, memo, size) for k in kid_keys)
    total = 1 + sum(size[k] for k in kid_keys)
    sig = node.get("type") or "?"
    if parts:
        sig += "(" + ",".join(parts) + ")"
    memo[key], size[key] = sig, total
    return sig


def _subtree_report(root: str, min_uses: int, min_size: int) -> None:
    """How much of the corpus is recombination of recurring SUBTREES.

    The per-component numbers above say the shape vocabulary is tiny, but a
    template library is made of sections, not single components. This asks the
    question a `build_section(spec)` tool actually depends on: take every subtree
    that recurs at least `min_uses` times and has at least `min_size`
    components, then greedily cover each page with the largest ones that match.
    The covered share is what a library could emit without inventing anything.

    Greedy top-down is deliberate: a matched subtree is emitted whole, so its
    descendants are not separately templated.
    """
    pages = list(_pages_with_trees(root))
    if not pages:
        return
    counts: collections.Counter = collections.Counter()
    per_page = []
    for label, cd in pages:
        memo: dict = {}
        size: dict = {}
        for r in _roots(cd):
            _subtree_sig(cd, r, memo, size)
        for k in cd:
            if k not in memo:
                _subtree_sig(cd, k, memo, size)
        counts.update(memo[k] for k in cd if size.get(k, 1) >= min_size)
        per_page.append((label, cd, memo, size))

    library = {sig for sig, n in counts.items() if n >= min_uses}
    total_comps = sum(len(cd) for _, cd, _, _ in per_page)

    covered = 0
    for _, cd, memo, size in per_page:
        stack = list(_roots(cd)) or list(cd)
        while stack:
            k = stack.pop()
            node = cd.get(k)
            if not isinstance(node, dict):
                continue
            if memo.get(k) in library:
                covered += size.get(k, 1)      # emitted whole, don't descend
                continue
            kids = node.get("children")
            if isinstance(kids, dict):
                stack.extend(kk for kk, v in kids.items() if v and kk in cd)

    print()
    print(f"Subtree templates — recurring >= {min_uses}x, >= {min_size} components")
    print(f"  distinct subtrees of that size : {len(counts):,}")
    print(f"  qualifying as templates        : {len(library):,}")
    print(f"  components coverable by them   : {covered:,} / {total_comps:,} "
          f"({covered / total_comps * 100:.1f}%)")
    print("  most reused sections:")
    for sig, n in counts.most_common(8):
        depth = sig.count("(")
        width = sig.count(",") + 1
        print(f"    {n:5d}x  {sig[:74]}{'…' if len(sig) > 74 else ''}  (~{width} parts, depth {depth})")


if __name__ == "__main__":
    sys.exit(main())
