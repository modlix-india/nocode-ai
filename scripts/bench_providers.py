#!/usr/bin/env python3
"""Provider benchmark — Gemini Flash vs Claude Haiku vs GPT-4o-mini.

Runs a fixed corpus of representative CFA conversations through each
candidate provider and records:
  - Turns to convergence
  - Tool-call accuracy (% succeeded without retry, % schema fetches OK)
  - Kirun DSL compile pass-rate (when the agent authored a function)
  - Per-app KB write success (propose-then-commit completes cleanly)
  - Wall-clock per conversation
  - $ per converged conversation (using each provider's published rates)

Output: scripts/bench_results/<timestamp>/{summary.md, raw.csv}.

This script is intentionally SKELETAL — the corpus and conversation runner
need to be hand-curated by the team running the bench. We provide:
  1. A clear conversation schema (`Conversation` dataclass).
  2. A clear metric model (`BenchMetrics` dataclass).
  3. A runner shell that loads the corpus from `bench_corpus.yaml` and
     dispatches per provider.

What's NOT yet implemented (and shouldn't be without real Gemini API access):
  - The corpus itself — write 10-15 conversations covering page CRUD,
    Kirun authoring, storage query, screenshot critique, KB read/write,
    code-workspace lookup.
  - The "did this converge?" oracle — likely a per-conversation checklist
    in YAML (e.g. {must_call_tools: [list_pages, create_page]}).
  - Cost arithmetic — provider-published rates change; keep them in
    `provider_costs.yaml` so the bench picks up new pricing automatically.

Run:
    ./venv/bin/python scripts/bench_providers.py --providers gemini,anthropic,openai
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


@dataclass
class Conversation:
    """One bench conversation. Comes from bench_corpus.yaml."""
    name: str
    description: str
    messages: list[str]  # User turns, fed one at a time
    must_call_tools: list[str] = field(default_factory=list)
    must_succeed_on_kirun: bool = False
    must_succeed_on_kb_write: bool = False


@dataclass
class BenchMetrics:
    """Per-(provider, conversation) result row."""
    provider: str
    conversation: str
    turns: int = 0
    tool_calls_total: int = 0
    tool_calls_succeeded: int = 0
    schema_fetches: int = 0
    schema_fetches_succeeded: int = 0
    kirun_compiles_total: int = 0
    kirun_compiles_succeeded: int = 0
    kb_writes_total: int = 0
    kb_writes_succeeded: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    wall_seconds: float = 0.0
    converged: bool = False
    failure_reason: Optional[str] = None

    def estimate_cost_usd(self, rates: dict[str, dict[str, float]]) -> float:
        r = rates.get(self.provider) or {}
        return (
            (self.input_tokens / 1_000_000) * r.get("input_per_million", 0.0)
            + (self.output_tokens / 1_000_000) * r.get("output_per_million", 0.0)
        )


# Published rates (USD per 1M tokens) at time of writing — adjust before each
# bench run. Better: pull from an env file or the provider's API.
_DEFAULT_RATES: dict[str, dict[str, float]] = {
    "gemini":     {"input_per_million": 0.075, "output_per_million": 0.30},
    "anthropic":  {"input_per_million": 1.00,  "output_per_million": 5.00},
    "openai":     {"input_per_million": 0.15,  "output_per_million": 0.60},  # gpt-4o-mini
    "deepseek":   {"input_per_million": 0.27,  "output_per_million": 1.10},
}


def _load_corpus(path: Path) -> list[Conversation]:
    """Load conversations from YAML or JSON. Returns empty list if missing."""
    if not path.exists():
        log.warning("Corpus file %s missing — bench will run with zero conversations.", path)
        return []
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-not-found]
        raw = yaml.safe_load(text)
    except ImportError:
        raw = json.loads(text)
    convs: list[Conversation] = []
    for entry in (raw or {}).get("conversations", []) or []:
        convs.append(Conversation(
            name=entry["name"],
            description=entry.get("description", ""),
            messages=list(entry.get("messages") or []),
            must_call_tools=list(entry.get("must_call_tools") or []),
            must_succeed_on_kirun=bool(entry.get("must_succeed_on_kirun")),
            must_succeed_on_kb_write=bool(entry.get("must_succeed_on_kb_write")),
        ))
    return convs


async def _run_one(provider_name: str, conv: Conversation) -> BenchMetrics:
    """Run one conversation through one provider. STUB — fills in the agent
    loop wiring once the team curates a real corpus + decides on the runner
    integration with BaseAgent.

    A real implementation would:
      1. Spin up BaseAgent with provider_name override.
      2. Drive each user turn through the agentic loop.
      3. Observe tool calls, count schema fetches via the meta_tools cache,
         observe kirun_dsl compiles, KB writes.
      4. Decide converged: every `must_call_tools` got called, every
         `must_succeed_*` flag held.
    """
    metrics = BenchMetrics(provider=provider_name, conversation=conv.name)
    metrics.failure_reason = (
        "STUB: bench runner not yet wired into BaseAgent — fill in _run_one "
        "with the actual agent-loop integration once the team curates a "
        "real corpus."
    )
    return metrics


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--providers", default="gemini,anthropic,openai",
                       help="Comma-separated providers to bench")
    parser.add_argument("--corpus", default="scripts/bench_corpus.yaml",
                       help="Path to the conversation corpus")
    parser.add_argument("--out-dir", default="scripts/bench_results",
                       help="Where to write summary.md + raw.csv")
    args = parser.parse_args()

    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    corpus = _load_corpus(Path(args.corpus))
    if not corpus:
        print(
            f"No conversations in {args.corpus}. Curate the bench corpus before running. "
            "Suggested coverage: page CRUD, Kirun function authoring, storage query, "
            "screenshot+critique, KB write, code-workspace lookup."
        )
        return 1

    out_dir = Path(args.out_dir) / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    all_rows: list[BenchMetrics] = []
    for provider_name in providers:
        for conv in corpus:
            log.info("Running %s × %s ...", provider_name, conv.name)
            t0 = time.monotonic()
            try:
                m = await _run_one(provider_name, conv)
            except Exception as e:  # noqa: BLE001
                m = BenchMetrics(provider=provider_name, conversation=conv.name)
                m.failure_reason = f"{type(e).__name__}: {e}"
            m.wall_seconds = time.monotonic() - t0
            all_rows.append(m)

    # Write CSV
    csv_path = out_dir / "raw.csv"
    headers = [f.name for f in dataclasses.fields(BenchMetrics)]
    lines = [",".join(headers)]
    for r in all_rows:
        lines.append(",".join(str(getattr(r, h, "")) for h in headers))
    csv_path.write_text("\n".join(lines), encoding="utf-8")

    # Write a one-page Markdown summary grouped by provider.
    summary = out_dir / "summary.md"
    md = ["# Provider bench results", "", f"Corpus: {len(corpus)} conversations × {len(providers)} providers", ""]
    for provider_name in providers:
        rows = [r for r in all_rows if r.provider == provider_name]
        if not rows:
            continue
        converged = sum(1 for r in rows if r.converged)
        total_cost = sum(r.estimate_cost_usd(_DEFAULT_RATES) for r in rows)
        total_secs = sum(r.wall_seconds for r in rows)
        md.append(f"## {provider_name}")
        md.append(f"- converged: {converged}/{len(rows)}")
        md.append(f"- total wall: {total_secs:.1f}s")
        md.append(f"- estimated $: ${total_cost:.4f}")
        if any(r.failure_reason for r in rows):
            md.append("- notable failures:")
            for r in rows:
                if r.failure_reason:
                    md.append(f"  - {r.conversation}: {r.failure_reason}")
        md.append("")
    summary.write_text("\n".join(md), encoding="utf-8")

    print(f"\nWrote {csv_path}")
    print(f"Wrote {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
