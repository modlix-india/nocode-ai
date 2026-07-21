"""Chain multiple v4_overnight sessions with fresh JWTs.

Each iteration:
  1. Refresh JWT.
  2. Spawn `cfa_v4_overnight.py --target <X>` as a subprocess.
  3. Wait for it to finish (or hit timeout).
  4. Check the latest run's severity history for `high=0` at the tail.
     - If yes → declare done, exit 0.
     - Else → next iteration.

Stops after `max_iterations` OR when the final compare returns high=0.
Designed for hands-off overnight runs where a 30-min JWT TTL otherwise
caps each session.

Usage:
  venv/bin/python scripts/cfa_v4_overnight_loop.py \\
      [--target linear|stripe|vercel] [--max-iterations 10]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx


REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO_ROOT / "scripts" / "cfa_runs" / "v4_overnight"
JWT_PATH = Path.home() / ".cfa-jwt"
CREDS_PATH = Path.home() / ".cfa-creds"
SAAS_BASE = os.environ.get("SAAS_BASE_URL", "http://localhost:8080")
FORWARDED_HOST = "localhost:8080"
FORWARDED_PORT = "8080"


async def _refresh_jwt() -> str:
    """Force a fresh login with the stored creds. Overwrites ~/.cfa-jwt."""
    creds = json.loads(CREDS_PATH.read_text())
    body = {
        "userName": creds["username"], "password": creds["password"],
        "identifierType": creds.get("identifierType", "EMAIL_ID"),
        "loggedInClientCode": creds.get("clientCode", "SYSTEM"),
    }
    headers = {"Content-Type": "application/json",
               "X-Forwarded-Host": FORWARDED_HOST, "X-Forwarded-Port": FORWARDED_PORT,
               "clientCode": creds.get("clientCode", "SYSTEM"),
               "appCode": creds.get("appCode", "appbuilder")}
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(f"{SAAS_BASE}/api/security/authenticate",
                          json=body, headers=headers)
    r.raise_for_status()
    tok = r.json()["accessToken"]
    JWT_PATH.write_text(tok); JWT_PATH.chmod(0o600)
    return tok


def _latest_session_summary(target: str) -> dict | None:
    """Read the latest cfa_runs/v4_overnight/<target>/<ts>/summary.txt
    and parse the severity history. Returns
    `{tail_high: int, tail_medium: int, total_compares: int}` or None."""
    target_dir = RUNS_DIR / target
    if not target_dir.exists():
        return None
    runs = sorted(target_dir.iterdir())
    if not runs:
        return None
    latest = runs[-1]
    summary_path = latest / "summary.txt"
    if not summary_path.exists():
        return None
    text = summary_path.read_text()
    import re as _re
    # Lines like "  # 6  high=2  medium=2  low=0"
    matches = _re.findall(r"high=(\d+)\s+medium=(\d+)\s+low=(\d+)", text)
    if not matches:
        return {"tail_high": None, "tail_medium": None, "total_compares": 0, "dir": str(latest)}
    last = matches[-1]
    return {
        "tail_high": int(last[0]),
        "tail_medium": int(last[1]),
        "tail_low": int(last[2]),
        "total_compares": len(matches),
        "dir": str(latest),
    }


def _run_one_session(target: str, timeout_s: int = 2700) -> int:
    """Spawn one overnight session, kill it on wall-clock timeout. Returns
    subprocess return code (124 on timeout). Uses Popen+wait+kill because
    `subprocess.run(timeout=...)` has been observed to NOT fire on
    long-running children in this setup."""
    # `-u` forces unbuffered stdout/stderr so the loop's tee sees output
    # in real time instead of after the child exits.
    cmd = [sys.executable, "-u", "scripts/cfa_v4_overnight.py", "--target", target]
    print(f"\n--- spawning  {' '.join(cmd)}", flush=True)
    started = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT))
    try:
        rc = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        print(f"  TIMEOUT after {timeout_s}s — killing session pid {proc.pid}", flush=True)
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print("  child did not respond to kill within 10s", flush=True)
        rc = 124
    elapsed = (time.monotonic() - started) / 60
    print(f"--- session ended in {elapsed:.1f} min  rc={rc}", flush=True)
    return rc


async def _wait_for_gateway_ready(max_wait_s: int = 120) -> bool:
    """Eureka client refreshes its cache every 30-60s. After a fresh
    nocode-ai start, requests routed via /api/ai/** can 503 until the
    gateway sees the registration. Poll the gateway's health route
    until it returns 200 — or give up after max_wait_s."""
    url = f"{os.environ.get('CFA_BASE_URL', 'http://localhost:8080')}/api/ai/health"
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=5.0) as c:
        while time.monotonic() - started < max_wait_s:
            try:
                r = await c.get(url)
                if r.status_code == 200:
                    print(f"  gateway routable ({(time.monotonic()-started):.0f}s)", flush=True)
                    return True
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(5)
    return False


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="linear",
                    choices=["linear", "stripe", "vercel"])
    ap.add_argument("--max-iterations", type=int, default=10,
                    help="Cap on session restarts")
    args = ap.parse_args()

    print(f"=== v4 overnight loop: target={args.target}, "
          f"max-iter={args.max_iterations} ===", flush=True)
    print("\n--- waiting for gateway → nocode-ai routing", flush=True)
    if not await _wait_for_gateway_ready():
        print("ERROR: gateway not routable after 120s. Exiting.", flush=True)
        return 1

    consecutive_fast_fails = 0
    for i in range(1, args.max_iterations + 1):
        print(f"\n========== iteration {i}/{args.max_iterations} "
              f"@ {_dt.datetime.now().strftime('%H:%M')} ==========", flush=True)
        try:
            tok = await _refresh_jwt()
            print(f"  fresh JWT (tail ...{tok[-10:]})", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  JWT refresh failed: {type(e).__name__}: {e}", flush=True)
            print("  sleeping 60s and retrying", flush=True)
            await asyncio.sleep(60)
            continue

        session_started = time.monotonic()
        _run_one_session(args.target)
        session_elapsed_min = (time.monotonic() - session_started) / 60

        # If sessions are crashing in under 30s, the gateway/agent is
        # broken — don't blast through all 12 iterations in a few seconds.
        if session_elapsed_min < 0.5:
            consecutive_fast_fails += 1
            backoff = min(60 * consecutive_fast_fails, 300)
            print(f"  session ended too fast ({session_elapsed_min*60:.1f}s) — "
                  f"likely gateway/agent failure. Backing off {backoff}s.", flush=True)
            if consecutive_fast_fails >= 3:
                print("  ERROR: 3 fast-fail sessions in a row. Aborting loop.", flush=True)
                return 1
            await asyncio.sleep(backoff)
            continue
        consecutive_fast_fails = 0

        summary = _latest_session_summary(args.target)
        if summary is None:
            print("  no summary written by session (probably crashed early)", flush=True)
            continue
        print(f"  latest session: {summary}", flush=True)
        # Pass bar: ~85-90% similarity. Pixel-perfect (high=0 AND medium=0)
        # is unachievable because vision compare always flags animations
        # and custom-font differences. high=0 AND medium<=2 is the bar.
        h, m = summary.get("tail_high"), summary.get("tail_medium")
        if h is not None and m is not None and h == 0 and m <= 2:
            print(f"\n*** target {args.target} CONVERGED "
                  f"(high={h}, medium={m}) after {i} session(s). ***")
            return 0

    print(f"\nLoop hit max iterations ({args.max_iterations}). "
          f"Final state: {_latest_session_summary(args.target)}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
