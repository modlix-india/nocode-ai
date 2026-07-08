"""Loader for the adzump real-scrape corpus. scrape/<site>.json = captured input
{url,html,network,image_positions}; scrape/<site>.<stage>.expected.json = blessed goldens.
Bless: BLESS_FIXTURES=1 venv/bin/python -m unittest <fixture test modules>"""

from __future__ import annotations

import glob
import json
import os

SCRAPE_DIR = os.path.join(os.path.dirname(__file__), "scrape")


def inputs() -> list[str]:
    """Captured input paths (excl. *.expected.json goldens)."""
    return sorted(p for p in glob.glob(os.path.join(SCRAPE_DIR, "*.json"))
                  if not p.endswith(".expected.json"))


def name(input_path: str) -> str:
    return os.path.splitext(os.path.basename(input_path))[0]


def parsed(input_path: str):
    """Real parse_html over a fixture → candidate-image list."""
    from app.agents.adzump.agents.product.adapters.html_parser import parse_html
    with open(input_path, encoding="utf-8") as f:
        fx = json.load(f)
    return parse_html(
        fx["url"], fx["html"],
        network_images=fx.get("network") or [],
        image_positions=fx.get("image_positions") or {},
    )


def check(case, got, input_path: str, stage: str) -> None:
    """Assert got == <site>.<stage>.expected.json; BLESS_FIXTURES=1 rewrites it instead."""
    gp = input_path[: -len(".json")] + f".{stage}.expected.json"
    if os.environ.get("BLESS_FIXTURES"):
        with open(gp, "w", encoding="utf-8") as f:
            json.dump(got, f, indent=2)
        return
    case.assertTrue(os.path.exists(gp),
                    f"no {stage} golden for {name(input_path)} - bless with BLESS_FIXTURES=1")
    with open(gp, encoding="utf-8") as f:
        want = json.load(f)
    case.assertEqual(got, want, f"{name(input_path)} [{stage}]: output changed vs golden")
