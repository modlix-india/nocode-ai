"""Post-run validation of every built app + clone via the MCP drive_page tool.

Usage:
    python scripts/validate_all_scenarios.py

For each app (taskmate, shopkeep): drives /home anonymously, expects the
login form (platform substitution), types sysadmin creds, clicks Sign In,
expects the home page rendered with token in localStorage.

For each clone (clone_linear, clone_stripe, clone_vercel): drives the
default page anonymously (clones are public; no auth required) and
screenshots top + mid + bottom.

The MCP `drive_page` tool is NOT importable from Python — it lives in the
Claude Code MCP server runtime. This script instead emits a JSON
manifest the human (or Claude Code) can step through, plus runs a
non-MCP equivalent via Playwright directly so we have CI-runnable
evidence in addition to the MCP screenshots.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "scripts" / "cfa_runs" / "_validation"


SCENARIOS = {
    "taskmate": {
        "kind": "app",
        "client_code": "SYSTEM",
        "app_code": "taskmate",
        "default_page": "home",
        "expects_login_wrapper": True,
        "username": "sysadmin@modlix.com",
        "password": "Pass@1234",
    },
    "shopkeep": {
        "kind": "app",
        "client_code": "SYSTEM",
        "app_code": "shopkeep",
        "default_page": "home",
        "expects_login_wrapper": True,
        "username": "sysadmin@modlix.com",
        "password": "Pass@1234",
    },
    "clone_linear": {
        "kind": "clone",
        "client_code": "SYSTEM",
        "app_code": "clone_linear",
        "default_page": "home",
        "expects_login_wrapper": False,
    },
    "clone_stripe": {
        "kind": "clone",
        "client_code": "SYSTEM",
        "app_code": "clone_stripe",
        "default_page": "home",
        "expects_login_wrapper": False,
    },
    "clone_vercel": {
        "kind": "clone",
        "client_code": "SYSTEM",
        "app_code": "clone_vercel",
        "default_page": "home",
        "expects_login_wrapper": False,
    },
}


async def _drive_app(scenario: dict[str, Any], outdir: Path) -> dict[str, Any]:
    """Drive an app via Playwright. Mirrors the MCP drive_page golden path:
    anon load → expect login → fill creds → click Sign In → expect home."""
    from playwright.async_api import async_playwright

    app_code = scenario["app_code"]
    client_code = scenario["client_code"]
    default_page = scenario["default_page"]
    url = f"https://apps.local.modlix.com/{app_code}/{client_code}/page/{default_page}"
    record: dict[str, Any] = {
        "scenario": app_code,
        "kind": scenario["kind"],
        "url": url,
        "steps": [],
    }

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, ignore_https_errors=True)
        page = await ctx.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        try:
            await page.goto(url, wait_until="networkidle", timeout=20000)
        except Exception:
            pass
        await page.wait_for_timeout(3500)
        title = await page.title()
        png1 = await page.screenshot(full_page=False, type="png")
        (outdir / f"{app_code}-step1-anon.png").write_bytes(png1)
        ins = await page.locator("input").count()
        btns = await page.locator("button").count()
        record["steps"].append({"step": "anon_load", "title": title, "input_count": ins, "button_count": btns})

        if scenario.get("expects_login_wrapper"):
            # Find inputs in order; type creds.
            if ins >= 2:
                inputs = page.locator("input")
                try:
                    await inputs.nth(0).fill(scenario["username"], timeout=5000)
                    await inputs.nth(1).fill(scenario["password"], timeout=5000)
                    record["steps"].append({"step": "filled_creds", "ok": True})
                except Exception as e:
                    record["steps"].append({"step": "filled_creds", "ok": False, "err": str(e)})

                # Click first button labeled Sign In (or any button).
                try:
                    btn = page.locator("button").filter(has_text="Sign").first
                    if not await btn.count():
                        btn = page.locator("button").first
                    await btn.click(timeout=5000)
                    record["steps"].append({"step": "clicked_signin", "ok": True})
                except Exception as e:
                    record["steps"].append({"step": "clicked_signin", "ok": False, "err": str(e)})

                await page.wait_for_timeout(3000)
                png2 = await page.screenshot(full_page=False, type="png")
                (outdir / f"{app_code}-step2-after-signin.png").write_bytes(png2)
                token = await page.evaluate("window.localStorage.getItem('modlixToken') || ''")
                final_title = await page.title()
                final_ins = await page.locator("input").count()
                final_btns = await page.locator("button").count()
                record["steps"].append({
                    "step": "post_signin",
                    "title": final_title,
                    "has_token": bool(token),
                    "input_count": final_ins,
                    "button_count": final_btns,
                })
            else:
                record["steps"].append({"step": "filled_creds", "ok": False, "err": f"only {ins} inputs found"})

        if page_errors:
            record["page_errors"] = page_errors[:10]

        await browser.close()

    return record


async def _drive_clone(scenario: dict[str, Any], outdir: Path) -> dict[str, Any]:
    """Drive a clone page anonymously; screenshot top/mid/bottom."""
    from playwright.async_api import async_playwright

    app_code = scenario["app_code"]
    client_code = scenario["client_code"]
    default_page = scenario["default_page"]
    url = f"https://apps.local.modlix.com/{app_code}/{client_code}/page/{default_page}"
    record: dict[str, Any] = {
        "scenario": app_code, "kind": "clone", "url": url, "steps": [],
    }

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, ignore_https_errors=True)
        page = await ctx.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        try:
            await page.goto(url, wait_until="networkidle", timeout=25000)
        except Exception:
            pass
        await page.wait_for_timeout(3500)
        title = await page.title()
        doc_h = await page.evaluate("document.documentElement.scrollHeight")
        view_h = 900
        for label, pos in (("top", 0.0), ("mid", 0.5), ("bottom", 1.0)):
            y = int(max(0.0, pos) * max(0, doc_h - view_h))
            await page.evaluate(f"window.scrollTo({{top: {y}, behavior: 'instant'}})")
            await page.wait_for_timeout(500)
            png = await page.screenshot(full_page=False, type="png")
            (outdir / f"{app_code}-{label}.png").write_bytes(png)
        record["steps"].append({"step": "loaded", "title": title, "doc_height": doc_h, "page_errors": page_errors[:5]})

        await browser.close()

    return record


async def _amain() -> int:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for name, scenario in SCENARIOS.items():
        outdir = RESULTS_DIR / name
        outdir.mkdir(exist_ok=True)
        if scenario["kind"] == "app":
            r = await _drive_app(scenario, outdir)
        else:
            r = await _drive_clone(scenario, outdir)
        results.append(r)
        print(json.dumps(r, indent=2))
    (RESULTS_DIR / "summary.json").write_text(json.dumps(results, indent=2))
    print(f"\nWrote summary + screenshots to {RESULTS_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
