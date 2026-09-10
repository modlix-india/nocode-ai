"""Bench: essence extraction quality/latency, gpt-4o-mini vs deepseek vision.

Decides whether the Essence Analyst moves off OpenAI (config.py note: vision
sub-agents stay on gpt-4o-mini until deepseek-v4-flash-vision-exp is benched).
DeepSeek would also stream reasoning into the observability card.

Uses REAL competitor ad creatives rehosted by recent runs (downloaded from the
local file server, no auth needed) and the REAL EssenceAnalyst path - same
prompt, chunking, parsing, and lenient-enum coercion as production.

Run:  source ~/.nocode-ai/variables.sh && python scripts/bench_essence.py
Cost: one batched vision call per model (~10 images each).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.adzump.agents.creative_essence import agent as essence_agent
from app.agents.adzump.agents.creative_essence.agent import EssenceAnalyst
from app.agents.adzump.creative_intelligence.enrich import CreativeImage
from app.agents.adzump.creative_intelligence.models import Creative
from app.core.session import AuthContext

BENCH_AUTH = AuthContext(token="bench", client_code="BENCH", client_id=0,
                         user_id=0, app_code="marketingai")

FILE_SERVER = "http://localhost:4321"
LOG_FILE = Path("logs/ai.log")
N_IMAGES = 10
MODELS = {
    "gpt-4o-mini": "openai:gpt-4o-mini",
    "deepseek-vision": "deepseek:deepseek-v4-flash-vision-exp",
}
REPORT = Path("logs/bench_essence_report.md")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("bench")


def pick_creative_urls() -> list[str]:
    """Up to N distinct rehosted creatives, spread across competitors (max 2
    each) so the bench sees varied ad styles, newest runs first."""
    urls = re.findall(
        r"/api/files/static/file/GRMEL/competitor-creatives/[^\s]+\.jpg",
        LOG_FILE.read_text(errors="replace"),
    )
    per_comp: dict[str, int] = {}
    picked: list[str] = []
    for u in reversed(list(dict.fromkeys(urls))):  # unique, newest first
        comp = u.rsplit("/", 1)[-1].split("_")[1] if "_" in u else u
        if per_comp.get(comp, 0) >= 2:
            continue
        per_comp[comp] = per_comp.get(comp, 0) + 1
        picked.append(u)
        if len(picked) >= N_IMAGES:
            break
    return picked


def download(urls: list[str]) -> list[CreativeImage]:
    images: list[CreativeImage] = []
    with httpx.Client(timeout=30) as client:
        for u in urls:
            r = client.get(FILE_SERVER + u)
            if r.status_code != 200 or not r.content:
                logger.warning("skip %s (%s)", u, r.status_code)
                continue
            h = hashlib.md5(r.content).hexdigest()
            images.append(CreativeImage(
                creative=Creative(creative_id=h, media_type="image",
                                  content_hash=h),
                data=r.content,
                content_type="image/jpeg",
            ))
    return images


class BenchAnalyst(EssenceAnalyst):
    """Captures the token usage _emit_finished would have streamed."""

    tokens_in = 0
    tokens_out = 0

    async def _emit_finished(self, parent_event_stream, run_start, status,
                             summary, tokens_in, tokens_out):
        self.tokens_in, self.tokens_out = tokens_in, tokens_out
        await super()._emit_finished(parent_event_stream, run_start, status,
                                     summary, tokens_in, tokens_out)


async def run_model(label: str, override: str, images: list[CreativeImage]):
    essence_agent.ESSENCE_MODEL_OVERRIDE = override
    analyst = BenchAnalyst()
    t0 = time.monotonic()
    essences = await analyst.extract(images, parent_event_stream=None,
                                     auth=BENCH_AUTH)
    wall = time.monotonic() - t0
    return {
        "label": label,
        "override": override,
        "wall_s": round(wall, 1),
        "parsed": len(essences),
        "total": len(images),
        "tokens_in": analyst.tokens_in,
        "tokens_out": analyst.tokens_out,
        "essences": {h: e.model_dump() for h, e in essences.items()},
    }


def field_fill(essences: dict) -> dict[str, int]:
    fills: dict[str, int] = {}
    for e in essences.values():
        for k, v in e.items():
            if v not in (None, "", []):
                fills[k] = fills.get(k, 0) + 1
    return fills


def main() -> None:
    urls = pick_creative_urls()
    images = download(urls)
    logger.info("benching on %d real creatives", len(images))
    if len(images) < 5:
        raise SystemExit("not enough creatives downloadable - aborting")

    results = [asyncio.run(run_model(label, override, images))
               for label, override in MODELS.items()]

    hashes = [ci.creative.content_hash for ci in images]
    lines = ["# Essence bench - gpt-4o-mini vs deepseek-v4-flash-vision-exp", ""]
    lines += [f"{len(images)} real ad creatives, one production extract() per model.", ""]
    lines += ["| metric | " + " | ".join(r["label"] for r in results) + " |",
              "|---|" + "---|" * len(results)]
    for metric, get in [
        ("wall clock", lambda r: f"{r['wall_s']}s"),
        ("verdicts parsed", lambda r: f"{r['parsed']}/{r['total']}"),
        ("tokens in", lambda r: str(r["tokens_in"])),
        ("tokens out", lambda r: str(r["tokens_out"])),
    ]:
        lines.append(f"| {metric} | " + " | ".join(get(r) for r in results) + " |")
    for r in results:
        lines += ["", f"## field fill - {r['label']}",
                  "```json", json.dumps(field_fill(r["essences"]), indent=1), "```"]
    lines += ["", "## side-by-side verdicts (eyeball round)"]
    for i, h in enumerate(hashes):
        lines.append(f"\n### image {i + 1} ({urls[i].rsplit('/', 1)[-1]})")
        for r in results:
            v = r["essences"].get(h)
            lines.append(f"- **{r['label']}**: "
                         + (json.dumps(v, ensure_ascii=False) if v else "(no verdict)"))
    REPORT.write_text("\n".join(lines))
    print("\n".join(lines[:12]))
    print(f"\nfull report: {REPORT}")


if __name__ == "__main__":
    main()
