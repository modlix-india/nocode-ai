#!/usr/bin/env python3
"""Standalone test harness for the deterministic page analyzer.

No LLM, no agent, no DB. Pure Playwright + CDP.

Usage:
    ./venv/bin/python scripts/test_page_analyzer.py --url https://iii.dev/ --stage m1
    ./venv/bin/python scripts/test_page_analyzer.py --url https://iii.dev/ --headed
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.page_analyzer import analyze_page  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger("test_page_analyzer")


def _print_summary(analysis) -> None:
    print("\n" + "=" * 70)
    print(f"URL:            {analysis.url}")
    print(f"Stage:          {analysis.stage}")
    print(f"Run dir:        {analysis.run_dir}")
    print(f"Total elements: {analysis.total_elements}")
    print("-" * 70)
    print(f"{'breakpoint':<10} {'w':>5} {'h':>5} {'observed':>9} {'visible':>8} {'media':>6}")
    for bp in analysis.breakpoints:
        print(
            f"{bp.name:<10} {bp.width:>5} {bp.height:>5} "
            f"{bp.observed_count:>9} {bp.visible_count:>8} {len(bp.active_media):>6}"
        )
    if analysis.breakpoints:
        print("-" * 70)
        print("Active media per breakpoint:")
        for bp in analysis.breakpoints:
            shown = bp.active_media[:6]
            more = f"  (+{len(bp.active_media) - len(shown)} more)" if len(bp.active_media) > len(shown) else ""
            print(f"  {bp.name}: {shown}{more}")

    # Spot-check: elements visible at desktop but hidden at mobile (responsive).
    names = [bp.name for bp in analysis.breakpoints]
    if "desktop" in names and "mobile" in names:
        swaps = [
            o
            for o in analysis.observations
            if o.visible_at("desktop") and not o.visible_at("mobile")
        ]
        only_mobile = [
            o
            for o in analysis.observations
            if o.visible_at("mobile") and not o.visible_at("desktop")
        ]
        print("-" * 70)
        print(f"Responsive: {len(swaps)} elements desktop-only, {len(only_mobile)} mobile-only")
        for o in swaps[:8]:
            print(f"  desktop-only: {o.mxa_id} <{o.tag}>")
        for o in only_mobile[:8]:
            print(f"  mobile-only:  {o.mxa_id} <{o.tag}>")

    if analysis.warnings:
        print("-" * 70)
        print("WARNINGS:")
        for w in analysis.warnings:
            print(f"  ! {w}")
    print("=" * 70 + "\n")


def _print_m2(analysis) -> None:
    import json

    sample = (analysis.extra or {}).get("authored_sample", {})
    print("\n" + "=" * 70)
    print(f"URL:      {analysis.url}")
    print(f"Run dir:  {analysis.run_dir}")
    rv = sample.get("root_custom_properties", {})
    print(f":root custom properties: {len(rv)}")
    for k in list(rv)[:10]:
        print(f"    {k}: {rv[k]}")
    for t in sample.get("targets", []):
        print("-" * 70)
        print(f"[{t['role']}] {t['mxa_id']}")
        authored = t.get("authored", {})
        for bp in ("desktop", "tablet", "mobile"):
            props = authored.get(bp, {})
            print(f"  authored@{bp} ({len(props)} props): "
                  f"{ {k: props[k] for k in list(props)[:6]} }")
        if t.get("hover"):
            print(f"  hover deltas: {t['hover']}")
        print("  -> Modlix styleProperties:")
        print("     " + json.dumps(t.get("style_properties", {}), indent=2).replace("\n", "\n     "))
    print("=" * 70 + "\n")


def _flatten(nodes):
    for n in nodes:
        yield n
        yield from _flatten(n.children)


def _print_tree(node, indent: int, lines: list, cap: int) -> None:
    if len(lines) >= cap:
        return
    label = node.component_type
    if node.recognized_as:
        label += f"~{node.recognized_as}"
    extra = ""
    if node.text:
        extra = f' "{node.text[:32]}"'
    elif node.src:
        extra = " [img]"
    elif node.href:
        extra = " [link]"
    lines.append(f"{'  ' * indent}- {label} <{node.tag}>{extra}")
    for c in node.children:
        _print_tree(c, indent + 1, lines, cap)


def _print_m3(analysis) -> None:
    print("\n" + "=" * 70)
    print(f"URL:      {analysis.url}")
    print(f"Run dir:  {analysis.run_dir}")
    print(f"Sections: {len(analysis.sections)}   kept nodes: {analysis.total_elements}")
    banner = (analysis.extra or {}).get("banner", {})
    if banner:
        print(f"Banner:   dismissed={banner.get('dismissed')} method={banner.get('method')} "
              f"btn={banner.get('button_text', '-')}")
    if analysis.root_custom_properties:
        print(f":root vars: {len(analysis.root_custom_properties)}")
    print("-" * 70)
    for s in analysis.sections:
        rect = s.rect
        dims = f"{rect.w}x{rect.h}" if rect else "?"
        shots = f"  shots={list(s.screenshots)}" if s.screenshots else ""
        print(f"\n### [{s.index}] {s.role.upper()}  '{s.name}'  ({dims})  roots={len(s.roots)}{shots}")
        if s.heading_text:
            print(f"    heading: {s.heading_text[:60]}")
        styled = sum(1 for n in _flatten(s.roots) if n.style_properties)
        if styled:
            print(f"    styled nodes: {styled}")
        lines: list = []
        for r in s.roots:
            _print_tree(r, 1, lines, cap=18)
        for ln in lines:
            print(ln)
    if analysis.warnings:
        print("\nWARNINGS:")
        for w in analysis.warnings:
            print(f"  ! {w}")
    print("=" * 70 + "\n")


def _print_full(analysis) -> None:
    def flat(n):
        yield n
        for c in n.children:
            yield from flat(c)

    nodes = list(flat(analysis.full_tree)) if analysis.full_tree else []
    styled = sum(1 for n in nodes if n.style_properties)
    hover = sum(1 for n in nodes for r in n.style_properties.values() if r.get("pseudoState") == "hover")
    print("\n" + "=" * 70)
    print(f"URL:        {analysis.url}")
    print(f"Run dir:    {analysis.run_dir}")
    print(f"Full DOM:   {len(nodes)} nodes ({styled} styled)")
    print(f"Hover rules:{hover}   keyframes:{len(analysis.keyframes)}   fonts:{len(analysis.font_faces)}   :root vars:{len(analysis.root_custom_properties)}")
    banner = (analysis.extra or {}).get("banner", {})
    if banner:
        print(f"Banner:     dismissed={banner.get('dismissed')} ({banner.get('method')})")
    print(f"\nOpen the reconstruction:\n  file://{analysis.run_dir}/preview.html\n")
    print("=" * 70 + "\n")


async def main(args: argparse.Namespace) -> None:
    logger.info("Analyzing %s (stage=%s)", args.url, args.stage)
    analysis = await analyze_page(
        args.url,
        out_dir=args.out,
        stage=args.stage,
        headless=not args.headed,
        wait_ms=args.wait_ms,
        use_llm=args.use_llm,
        in_path=args.in_path,
    )
    if args.stage == "render":
        print(f"\nwrote preview.html in {analysis.run_dir}\n  open: file://{analysis.run_dir}/preview.html\n")
    elif args.stage == "m2":
        _print_m2(analysis)
    elif args.stage == "full":
        _print_full(analysis)
    elif args.stage in ("m3", "m4", "m5", "all"):
        _print_m3(analysis)
    else:
        _print_summary(analysis)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="", help="page to analyze (not needed for --stage render)")
    p.add_argument("--out", default=None, help="output run dir (default: runs/page_analyzer/<slug>_<ts>)")
    p.add_argument("--in", dest="in_path", default=None, help="existing analysis.json for --stage render")
    p.add_argument("--stage", default="m1", help="pipeline stage: m1 | m2 | m3 | m4 | m5 | all | render")
    p.add_argument("--headed", action="store_true", help="run a visible browser")
    p.add_argument("--wait-ms", type=int, default=2500, help="post-load settle wait")
    p.add_argument("--use-llm", action="store_true", help="allow LLM-vision cookie-banner fallback")
    args = p.parse_args()
    asyncio.run(main(args))
