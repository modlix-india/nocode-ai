"""Per-run failure classifier for cfa_drive scenarios.

Reads a turns.jsonl file produced by cfa_drive.py and groups failures into
actionable buckets:

  * tool_failure        - tool returned success=false or HTTP error
  * http_error          - the chat endpoint itself returned non-200
  * sse_error           - agent emitted an `error` SSE event mid-stream
  * doom_loop           - same (tool, args) repeated >= REPEAT_THRESHOLD times
  * propose_no_commit   - propose_kb_update without a matching commit_kb_update
  * turn_overflow       - turn exceeded SOFT_TURN_BUDGET tool calls
  * scope_drift         - a turn called >= SCOPE_DRIFT_THRESHOLD distinct tools

Usage:
    python scripts/cfa_triage.py scripts/cfa_runs/taskmate/latest
    python scripts/cfa_triage.py scripts/cfa_runs/taskmate/latest/turns.jsonl

Prints a per-bucket table and exits non-zero if any bucket is non-empty.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPEAT_THRESHOLD = 3
SOFT_TURN_BUDGET = 18
SCOPE_DRIFT_THRESHOLD = 14


@dataclass
class _Finding:
    bucket: str
    turn: int
    detail: str
    extra: dict[str, Any] = field(default_factory=dict)


def _load_events(path: Path) -> list[dict[str, Any]]:
    if path.is_dir():
        candidate = path / "turns.jsonl"
        if not candidate.exists():
            sys.exit(f"no turns.jsonl in {path}")
        path = candidate
    if not path.exists():
        sys.exit(f"not found: {path}")
    out: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _arg_signature(args: Any) -> str:
    if args is None:
        return ""
    try:
        return json.dumps(args, sort_keys=True)[:300]
    except (TypeError, ValueError):
        return repr(args)[:300]


def _group_by_turn(events: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    by_turn: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        t = e.get("turn")
        if isinstance(t, int):
            by_turn[t].append(e)
    return by_turn


@dataclass
class _TurnRollup:
    in_flight: dict[str, dict[str, Any]] = field(default_factory=dict)
    sig_counts: Counter[tuple[str, str]] = field(default_factory=Counter)
    propose_ids: set[str] = field(default_factory=set)
    commit_ids: set[str] = field(default_factory=set)
    distinct_tools: set[str] = field(default_factory=set)
    tool_call_total: int = 0
    failures: list[_Finding] = field(default_factory=list)


def _payload_tool(payload: dict[str, Any]) -> str:
    return payload.get("tool_name") or payload.get("tool") or ""


def _on_tool_start(payload: dict[str, Any], rollup: _TurnRollup) -> None:
    tool = _payload_tool(payload)
    tu_id = payload.get("tool_use_id") or ""
    rollup.in_flight[tu_id] = payload
    rollup.distinct_tools.add(tool)
    rollup.tool_call_total += 1
    rollup.sig_counts[(tool, _arg_signature(payload.get("tool_input") or payload.get("input")))] += 1
    if tool == "propose_kb_update":
        rollup.propose_ids.add(tu_id)


def _on_tool_result(turn: int, payload: dict[str, Any], rollup: _TurnRollup) -> None:
    tu_id = payload.get("tool_use_id") or ""
    started = rollup.in_flight.pop(tu_id, {})
    tool = _payload_tool(started) or _payload_tool(payload)
    if tool == "commit_kb_update" and payload.get("success"):
        rollup.commit_ids.add(tu_id)
    if payload.get("success") is False:
        rollup.failures.append(
            _Finding(
                bucket="tool_failure",
                turn=turn,
                detail=f"{tool}: {(payload.get('error') or payload.get('summary') or '')[:160]}",
                extra={"tool": tool, "args": started.get("tool_input") or started.get("input")},
            )
        )


def _emit_threshold_findings(turn: int, rollup: _TurnRollup) -> list[_Finding]:
    out: list[_Finding] = []
    for sig, count in rollup.sig_counts.items():
        if count >= REPEAT_THRESHOLD:
            out.append(
                _Finding(
                    bucket="doom_loop",
                    turn=turn,
                    detail=f"{sig[0]} called {count}x with same args",
                    extra={"tool": sig[0], "count": count},
                )
            )
    if len(rollup.propose_ids) > len(rollup.commit_ids):
        out.append(
            _Finding(
                bucket="propose_no_commit",
                turn=turn,
                detail=f"{len(rollup.propose_ids)} propose_kb_update calls, only {len(rollup.commit_ids)} commits",
            )
        )
    if rollup.tool_call_total >= SOFT_TURN_BUDGET:
        out.append(
            _Finding(
                bucket="turn_overflow",
                turn=turn,
                detail=f"{rollup.tool_call_total} tool calls in one turn (soft budget {SOFT_TURN_BUDGET})",
            )
        )
    if len(rollup.distinct_tools) >= SCOPE_DRIFT_THRESHOLD:
        out.append(
            _Finding(
                bucket="scope_drift",
                turn=turn,
                detail=f"{len(rollup.distinct_tools)} distinct tools touched in one turn",
                extra={"tools": sorted(rollup.distinct_tools)},
            )
        )
    return out


def _classify_tool_events(turn: int, events: list[dict[str, Any]]) -> list[_Finding]:
    rollup = _TurnRollup()
    findings: list[_Finding] = []
    for e in events:
        name = e.get("event")
        payload = e.get("payload") or {}
        if name == "tool_start":
            _on_tool_start(payload, rollup)
        elif name == "tool_result":
            _on_tool_result(turn, payload, rollup)
        elif name == "http_error":
            findings.append(_Finding(bucket="http_error", turn=turn, detail=f"HTTP {e.get('status')} {e.get('body','')[:160]}"))
        elif name == "error":
            findings.append(_Finding(bucket="sse_error", turn=turn, detail=str(payload)[:200]))
    findings.extend(rollup.failures)
    findings.extend(_emit_threshold_findings(turn, rollup))
    return findings


def _print_report(findings: list[_Finding], total_turns: int) -> None:
    if not findings:
        print(f"OK: {total_turns} turns, zero findings.")
        return
    by_bucket: dict[str, list[_Finding]] = defaultdict(list)
    for f in findings:
        by_bucket[f.bucket].append(f)
    order = [
        "http_error",
        "sse_error",
        "tool_failure",
        "doom_loop",
        "propose_no_commit",
        "turn_overflow",
        "scope_drift",
    ]
    print(f"\nTriage report — {total_turns} turns, {len(findings)} findings")
    for bucket in order:
        rows = by_bucket.get(bucket) or []
        if not rows:
            continue
        print(f"\n  [{bucket}] x{len(rows)}")
        for r in rows[:10]:
            print(f"    turn {r.turn}: {r.detail}")
        if len(rows) > 10:
            print(f"    ... +{len(rows) - 10} more")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="run dir or turns.jsonl path")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    events = _load_events(Path(args.path))
    by_turn = _group_by_turn(events)
    findings: list[_Finding] = []
    for turn, turn_events in sorted(by_turn.items()):
        findings.extend(_classify_tool_events(turn, turn_events))

    _print_report(findings, total_turns=len(by_turn))
    return 0 if not findings else 2


if __name__ == "__main__":
    sys.exit(main())
