"""Bench: VisionAnalyst pick quality/latency, gpt-4o-mini vs deepseek vision.

Decides whether the asset picker follows the Essence Analyst off OpenAI
(vision/agent.py note: a DeepSeek bench is pending). The essence bench
(scripts/bench_essence.py, 2026-09-10) showed deepseek grounds on-image text
where gpt-4o-mini fabricated - this checks the SELECT task: logo vs partner
logo vs hero vs floor plan.

Uses REAL scrapes of the real-estate test sites through the production
Playwright adapter, then the REAL select path - same prefilter, thumbnail
fetch, metadata JSON, screenshot-as-block-#0, prompt, parsing, and
_resolve_picks as production.

Run:  source ~/.nocode-ai/variables.sh && python scripts/bench_vision.py
Cost: one vision call per site per model (2 sites x 2 models).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.adzump.agents.product import product_assets
from app.agents.adzump.agents.product.adapters.playwright_adapter import scrape_page
from app.agents.adzump.agents.vision import agent as vision_agent
from app.agents.adzump.agents.vision.agent import VisionAnalyst
from app.core.session import AuthContext
from app.core.streaming import AgentEventStream

BENCH_AUTH = AuthContext(token="bench", client_code="BENCH", client_id=0,
                         user_id=0, app_code="marketingai")

SITES = ["https://cityville.in", "https://purvasparklingspring.com"]
MODELS = {
    # (provider, model_override, max_tokens) - deepseek gets headroom because
    # its reasoning stream shares the output budget; noted in the report.
    "gpt-4o-mini": ("openai", "openai:gpt-4o-mini", 600),
    # 2000 truncated on a 21-candidate site (reasoning ate the budget, JSON
    # never completed -> empty picks); 6000 gives the reasoning room.
    "deepseek-vision": ("deepseek", "deepseek:deepseek-v4-flash-vision-exp", 6000),
}
REPORT = Path("logs/bench_vision_report.md")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("bench")


class BenchVision(VisionAnalyst):
    """Captures the usage _emit_finished would have streamed."""

    def __init__(self) -> None:
        super().__init__(name="vision_select")
        self.tokens_in = self.tokens_out = 0
        self.status = ""

    async def _emit_finished(self, parent_event_stream, run_start, sub_session,
                             status, summary):
        usage = sub_session.total_usage or {}
        self.tokens_in = int(usage.get("input_tokens") or 0)
        self.tokens_out = int(usage.get("output_tokens") or 0)
        self.status = status
        await super()._emit_finished(parent_event_stream, run_start,
                                     sub_session, status, summary)


def _filename(url: str) -> str:
    return url.rsplit("/", 1)[-1].split("?", 1)[0][:60]


def _crude_summary(page) -> str:
    """Deterministic stand-in for the Profile Writer's summary - identical
    input for both models."""
    bits = [page.title or "", page.meta_description or ""]
    bits += page.headings[:3]
    return " · ".join(b.strip() for b in bits if b.strip())[:600]


async def prepare_site(url: str) -> dict | None:
    """One production-shaped input bundle per site, built ONCE and reused for
    every model so the comparison is apples-to-apples."""
    logger.info("scraping %s", url)
    result = await scrape_page(url)
    if not result.success or result.content is None:
        logger.warning("scrape failed for %s: %s", url, result.error)
        return None
    page = result.content
    candidates = product_assets._prefilter_candidates(
        page.images, product_assets.TOP_N_CANDIDATES)
    fetched = await product_assets._fetch_candidates(candidates)
    available = [c for c in candidates if c.src in fetched]
    if not available:
        logger.warning("no fetchable candidates for %s", url)
        return None
    return {
        "url": url,
        "summary": _crude_summary(page),
        "candidates": available,
        "fetched": fetched,
        "meta_json": product_assets._render_candidate_meta(available),
        "screenshot_b64": result.screenshot or None,
    }


async def run_model(label: str, site: dict) -> dict:
    provider, override, max_tokens = MODELS[label]
    vision_agent.VISION_PROVIDER = provider
    vision_agent.VISION_MODEL_OVERRIDE = override
    vision_agent.VISION_MAX_TOKENS = max_tokens
    analyst = BenchVision()  # fresh instance - provider binds at construction

    t0 = time.monotonic()
    assets = await analyst.pick(
        candidates=site["candidates"],
        fetched=site["fetched"],
        summary=site["summary"],
        meta_json=site["meta_json"],
        parent_event_stream=AgentEventStream(),  # queue-backed, unconsumed
        auth=BENCH_AUTH,
        full_page_screenshot_b64=site["screenshot_b64"],
    )
    wall = time.monotonic() - t0
    return {
        "label": label,
        "wall_s": round(wall, 1),
        "status": analyst.status,
        "tokens_in": analyst.tokens_in,
        "tokens_out": analyst.tokens_out,
        "confidence": assets.confidence,
        "note": assets.note,
        "logos": [{"file": _filename(l.url), "role": l.role,
                   "background": l.background, "reasoning": l.reasoning}
                  for l in assets.logos],
        "creatives": [{"file": _filename(c.url), "role": c.role,
                       "reasoning": c.reasoning}
                      for c in assets.creatives_with_role],
        "completeness": assets.creative_completeness.model_dump(),
    }


def _section(site: dict, runs: list[dict]) -> list[str]:
    lines = [f"\n## {site['url']}", "",
             f"{len(site['candidates'])} candidates after prefilter+fetch "
             f"(screenshot block #0: {'yes' if site['screenshot_b64'] else 'NO'})",
             "", "Candidates (index order the models saw):", ""]
    for i, c in enumerate(site["candidates"]):
        lines.append(f"- [{i}] `{_filename(c.src)}` alt={c.alt!r} src={c.source}")
    lines += ["", "| metric | " + " | ".join(r["label"] for r in runs) + " |",
              "|---|" + "---|" * len(runs)]
    for metric, get in [
        ("wall clock", lambda r: f"{r['wall_s']}s"),
        ("status", lambda r: r["status"]),
        ("tokens in / out", lambda r: f"{r['tokens_in']} / {r['tokens_out']}"),
        ("logo picks", lambda r: str(len(r["logos"]))),
        ("creative picks", lambda r: str(len(r["creatives"]))),
        ("completeness", lambda r: r["completeness"]["verdict"]),
        ("confidence", lambda r: str(r["confidence"])),
    ]:
        lines.append(f"| {metric} | " + " | ".join(get(r) for r in runs) + " |")
    for r in runs:
        lines += ["", f"### picks - {r['label']}", "```json",
                  json.dumps({"logos": r["logos"], "creatives": r["creatives"],
                              "note": r["note"]},
                             indent=1, ensure_ascii=False),
                  "```"]
    return lines


async def main() -> None:
    sites = [s for s in [await prepare_site(u) for u in SITES] if s]
    if not sites:
        raise SystemExit("no site scraped successfully - aborting")

    lines = ["# Vision-select bench - gpt-4o-mini vs deepseek-v4-flash-vision-exp",
             "",
             "One production pick() per site per model, identical inputs. "
             "Config note: deepseek runs with max_tokens=2000 (reasoning "
             "shares the output budget); gpt-4o-mini keeps production's 600."]
    for site in sites:
        runs = [await run_model(label, site) for label in MODELS]
        lines += _section(site, runs)
    REPORT.write_text("\n".join(lines))
    print("\n".join(lines[:6]))
    print(f"\nfull report: {REPORT}")


if __name__ == "__main__":
    asyncio.run(main())
